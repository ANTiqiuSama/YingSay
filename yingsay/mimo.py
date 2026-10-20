"""小米 MiMo OpenAI 兼容接口客户端（ASR / Chat / 全模态音频）。"""
from __future__ import annotations

import base64
import logging
import re
import time

import requests
import requests.adapters

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class MiMoError(Exception):
    pass


def _extract_text(resp: dict) -> str:
    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise MiMoError(f"接口返回格式异常: {str(resp)[:300]}")
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = content or ""
    # 防御：去掉可能混入的思考内容
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    return content.strip()


def wav_data_url(audio: bytes, mime: str = "audio/wav") -> str:
    return f"data:{mime};base64," + base64.b64encode(audio).decode("ascii")


class MiMoClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60, retries: int = 2):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._last_io = 0.0

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "api-key": self.api_key}

    def warmup(self, min_interval: float = 20.0) -> None:
        """提前建立 TLS 连接并放进连接池（按下热键时调用），省掉识别时的握手耗时。"""
        if not self.api_key or not self.base_url or time.monotonic() - self._last_io < min_interval:
            return
        self._last_io = time.monotonic()
        try:
            self.session.get(self.base_url + "/models", headers=self._headers(), timeout=5)
        except requests.RequestException:
            pass

    def probe(self, n: int = 2) -> tuple[float | None, int | None]:
        """测量到该节点的往返时延（秒）与鉴权状态码。"""
        best, code = None, None
        for _ in range(n):
            t0 = time.monotonic()
            try:
                r = self.session.get(self.base_url + "/models", headers=self._headers(), timeout=8)
                code = r.status_code
            except requests.RequestException:
                continue
            dt = time.monotonic() - t0
            best = dt if best is None else min(best, dt)
        return best, code

    # ------------------------------------------------------------ core
    def _post(self, payload: dict) -> dict:
        if not self.api_key:
            raise MiMoError("未配置 API Key，请在设置中填写（Token Plan 的 tp- 开头密钥）")
        if not self.base_url:
            raise MiMoError("未配置 Base URL")
        url = self.base_url + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
                self._last_io = time.monotonic()
            except requests.RequestException as e:
                last = MiMoError(f"网络错误: {e}")
            else:
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError:
                        raise MiMoError(f"接口返回非 JSON: {r.text[:300]}")
                detail = r.text[:400]
                try:
                    j = r.json()
                    detail = (j.get("error") or {}).get("message") or j.get("message") or detail
                except ValueError:
                    pass
                if r.status_code in (401, 403):
                    raise MiMoError(f"鉴权失败({r.status_code})：请检查 API Key 与 Base URL 是否匹配。{detail}")
                last = MiMoError(f"HTTP {r.status_code}: {detail}")
                if r.status_code not in RETRY_STATUS:
                    raise last
            if attempt < self.retries:
                time.sleep(0.6 * (2 ** attempt))
        raise last  # type: ignore[misc]

    # ------------------------------------------------------------ APIs
    def chat(self, model: str, messages: list, thinking: bool = False,
             max_tokens: int = 2048, temperature: float | None = 0.2) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "thinking": {"type": "enabled" if thinking else "disabled"},
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        return _extract_text(self._post(payload))

    def transcribe(self, audio: bytes, model: str = "mimo-v2.5-asr", language: str = "auto",
                   mime: str = "audio/wav") -> str:
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [{"type": "input_audio", "input_audio": {"data": wav_data_url(audio, mime)}}],
            }],
            "asr_options": {"language": language or "auto"},
            "stream": False,
        }
        return _extract_text(self._post(payload))

    def audio_chat(self, model: str, audio: bytes, system: str, instruction: str,
                   thinking: bool = False, max_tokens: int = 2048, mime: str = "audio/wav") -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": wav_data_url(audio, mime)}},
                {"type": "text", "text": instruction},
            ]},
        ]
        return self.chat(model, messages, thinking=thinking, max_tokens=max_tokens)
