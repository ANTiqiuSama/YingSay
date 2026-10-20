"""本地模型后端：通过 WSL2 里的 vLLM / sherpa-onnx 服务做识别与润色（完全离线、无网络往返）。

服务（由 local/setup_wsl.sh 安装，~/.yingsay-local/run.sh 启动）：
  8765  Qwen3-ASR（vLLM，OpenAI 兼容 chat.completions + audio_url）   served-model-name=local-asr
  8766  本地 LLM（vLLM，Qwen3-4B-Instruct-2507）                       served-model-name=local-llm
  8767  SenseVoice-Small（sherpa-onnx，POST /transcribe WAV）
"""
from __future__ import annotations

import base64
import logging
import re
import subprocess
import sys
import threading
import time

import requests

from .audio import to_wav_bytes

log = logging.getLogger(__name__)

LOCAL_ASR_MODELS = {
    "qwen3-asr-1.7b": "Qwen/Qwen3-ASR-1.7B",
    "qwen3-asr-0.6b": "Qwen/Qwen3-ASR-0.6B",
    "sensevoice": "none",  # 只用 SenseVoice，不占显存
}
LOCAL_LLM_MODELS = ["Qwen/Qwen3-4B-Instruct-2507", "Qwen/Qwen3-8B-FP8", "关闭"]
LANG_NAMES = {"zh": "Chinese", "en": "English"}
SETUP_URL = "https://raw.githubusercontent.com/ANTiqiuSama/YingSay/main/local/setup_wsl.sh"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
NEW_CONSOLE = 0x00000010 if sys.platform == "win32" else 0


class LocalError(Exception):
    pass


def parse_qwen_asr(content: str) -> str:
    """Qwen3-ASR 原始输出形如 'language Chinese<asr_text>你好。'，取出正文。"""
    content = content or ""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    if "<asr_text>" in content:
        head, content = content.split("<asr_text>", 1)
        if head.strip().lower() in ("language none", "none"):
            return ""
    content = content.replace("</asr_text>", "")
    m = re.match(r"^\s*language\s+(None|[A-Z][a-z]+)\s*$", content)
    if m:  # 只有语言标签、没有正文 = 没听到内容
        return ""
    return content.strip()


class LocalBackend:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = requests.Session()
        self._status = {"sv": False, "asr": False, "llm": False}
        self._checked = 0.0
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.installed: bool | None = None
        self.starting_since: float | None = None

    # ------------------------------------------------------------ 地址
    def _url(self, key: str, path: str) -> str:
        host = self.cfg.get("local_host") or "127.0.0.1"
        port = int(self.cfg.get(f"local_{key}_port") or {"asr": 8765, "llm": 8766, "sv": 8767}[key])
        return f"http://{host}:{port}{path}"

    # ------------------------------------------------------------ 状态
    def refresh(self, max_age: float = 3.0) -> dict:
        if time.monotonic() - self._checked < max_age:
            return dict(self._status)
        with self._lock:
            st = {}

            def probe(key, path):
                try:
                    st[key] = requests.get(self._url(key, path), timeout=0.5).status_code == 200
                except requests.RequestException:
                    st[key] = False

            ts = [threading.Thread(target=probe, args=kp, daemon=True)
                  for kp in (("sv", "/health"), ("asr", "/v1/models"), ("llm", "/v1/models"))]
            for t in ts:
                t.start()
            for t in ts:
                t.join(1.0)
            st = {k: bool(st.get(k)) for k in ("sv", "asr", "llm")}
            self._status, self._checked = st, time.monotonic()
            if any(st.values()):
                self.installed = True
            if st["asr"] and self.starting_since:
                log.info("本地模型就绪（%.0fs）", time.monotonic() - self.starting_since)
                self.starting_since = None
        return dict(st)

    def start_monitor(self):
        """后台定时探测本地服务，热路径只读缓存，不让 Windows 连接被拒的 ~2s 等待拖慢云端用户。"""
        def loop():
            while True:
                try:
                    if (self.cfg.get("asr_backend") or "auto") != "mimo" or \
                            (self.cfg.get("llm_backend") or "auto") != "mimo":
                        self.refresh(0)
                except Exception:  # noqa: BLE001
                    pass
                up = any(self._status.values()) or self.starting_since
                time.sleep(2 if up else (5 if self.installed else 15))

        threading.Thread(target=loop, daemon=True, name="local-monitor").start()

    def cached(self) -> dict:
        return dict(self._status)

    def asr_choice(self) -> str | None:
        """返回当前可用的本地识别器：'qwen' / 'sensevoice' / None。Qwen 还在加载时先用 SenseVoice 顶上。"""
        st = self.cached()
        want = self.cfg.get("local_asr_model") or "qwen3-asr-1.7b"
        if want != "sensevoice" and st["asr"]:
            return "qwen"
        if st["sv"]:
            return "sensevoice"
        return None

    def llm_ready(self) -> bool:
        return (self.cfg.get("local_llm_model") or "") != "关闭" and self.cached()["llm"]

    def describe(self) -> str:
        st = self.cached()
        if not any(st.values()):
            if self.starting_since:
                return f"本地模型：启动中（{time.monotonic() - self.starting_since:.0f}s）"
            return "本地模型：未启动" if self.installed is not False else "本地模型：未安装"
        want = self.cfg.get("local_asr_model") or "qwen3-asr-1.7b"
        asr = ("Qwen3-ASR" if st["asr"] else ("SenseVoice（Qwen 加载中）" if want != "sensevoice" else "SenseVoice"))
        llm = "本地润色✓" if st["llm"] else ("本地润色加载中" if self.starting_since and
                                         (self.cfg.get("local_llm_model") or "") != "关闭" else "本地润色✗")
        return f"本地模型：{asr} · {llm}"

    # ------------------------------------------------------------ 推理
    def transcribe(self, pcm, which: str, language: str = "auto", context: list[str] | None = None) -> str:
        wav = to_wav_bytes(pcm)
        timeout = 15 + len(pcm) / 16000 * 0.5
        if which == "sensevoice":
            r = self.session.post(self._url("sv", "/transcribe"), data=wav,
                                  headers={"Content-Type": "audio/wav"}, timeout=timeout)
            if r.status_code != 200:
                raise LocalError(f"SenseVoice 出错: {r.text[:200]}")
            return (r.json().get("text") or "").strip()
        messages = []
        if context:
            messages.append({"role": "system", "content": "、".join(context[:200])})
        messages.append({"role": "user", "content": [{
            "type": "audio_url",
            "audio_url": {"url": "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")},
        }]})
        payload = {"model": "local-asr", "messages": messages, "temperature": 0.0,
                   "max_tokens": int(256 + len(pcm) / 16000 * 12)}
        if language in LANG_NAMES:  # 强制语言：预填助手回复前缀
            messages.append({"role": "assistant", "content": f"language {LANG_NAMES[language]}<asr_text>"})
            payload.update({"continue_final_message": True, "add_generation_prompt": False})
        r = self.session.post(self._url("asr", "/v1/chat/completions"), json=payload, timeout=timeout)
        if r.status_code != 200 and context:
            log.info("本地 ASR 不接受上下文提示，去掉词典重试: %s", r.text[:120])
            payload["messages"] = [m for m in messages if m["role"] != "system"]
            r = self.session.post(self._url("asr", "/v1/chat/completions"), json=payload, timeout=timeout)
        if r.status_code != 200:
            raise LocalError(f"本地 Qwen3-ASR 出错 HTTP {r.status_code}: {r.text[:200]}")
        content = r.json()["choices"][0]["message"].get("content") or ""
        if language in LANG_NAMES and "<asr_text>" not in content:
            content = "<asr_text>" + content
        return parse_qwen_asr(content)

    def chat(self, messages: list, max_tokens: int = 1024, temperature: float = 0.2) -> str:
        payload = {"model": "local-llm", "messages": messages, "max_tokens": max_tokens,
                   "temperature": temperature, "chat_template_kwargs": {"enable_thinking": False}}
        r = self.session.post(self._url("llm", "/v1/chat/completions"), json=payload, timeout=60)
        if r.status_code != 200:
            raise LocalError(f"本地 LLM 出错 HTTP {r.status_code}: {r.text[:200]}")
        content = r.json()["choices"][0]["message"].get("content") or ""
        return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()

    # ------------------------------------------------------------ WSL 进程管理
    def _wsl(self, script: str) -> list[str]:
        cmd = ["wsl.exe"]
        distro = (self.cfg.get("local_wsl_distro") or "").strip()
        if distro:
            cmd += ["-d", distro]
        return cmd + ["-e", "bash", "-lc", script]

    def check_installed(self) -> bool:
        if sys.platform != "win32":
            self.installed = False
            return False
        try:
            out = subprocess.run(self._wsl("test -x ~/.yingsay-local/run.sh && echo YS_OK"), capture_output=True,
                                 timeout=25, creationflags=NO_WINDOW)
            self.installed = b"YS_OK" in out.stdout
        except Exception as e:  # noqa: BLE001
            log.info("检测 WSL 失败: %s", e)
            self.installed = False
        return self.installed

    def start(self) -> str:
        if self.refresh(0)["asr"]:
            return "本地模型已在运行"
        if self.proc and self.proc.poll() is None:
            return "本地模型正在启动…"
        if self.installed is None:
            self.check_installed()
        if not self.installed:
            return "还没有安装本地模型：请在设置 → 本地模型 里点「一键安装」"
        env = [f"YINGSAY_ASR_MODEL={LOCAL_ASR_MODELS.get(self.cfg.get('local_asr_model') or '', 'Qwen/Qwen3-ASR-1.7B')}"]
        llm = self.cfg.get("local_llm_model") or LOCAL_LLM_MODELS[0]
        env.append("YINGSAY_ENABLE_LLM=0" if llm == "关闭" else f"YINGSAY_LLM_MODEL={llm} YINGSAY_ENABLE_LLM=1")
        try:
            self.proc = subprocess.Popen(self._wsl(" ".join(env) + " ~/.yingsay-local/run.sh"),
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except Exception as e:  # noqa: BLE001
            return f"启动失败：{e}"
        self.starting_since = time.monotonic()
        log.info("已启动本地模型服务（WSL）")
        return "本地模型启动中：SenseVoice 几秒内可用，Qwen3-ASR 首次加载约 1-2 分钟"

    def stop(self) -> str:
        try:
            subprocess.run(self._wsl("~/.yingsay-local/stop.sh"), capture_output=True, timeout=20,
                           creationflags=NO_WINDOW)
        except Exception as e:  # noqa: BLE001
            log.info("停止本地模型失败: %s", e)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc, self.starting_since, self._checked = None, None, 0.0
        return "本地模型已停止，显存已释放"

    def restart(self) -> str:
        self.stop()
        time.sleep(1.5)
        return self.start()

    def open_installer(self) -> str:
        if sys.platform != "win32":
            return "仅支持 Windows + WSL2"
        script = (f"curl -fsSL {SETUP_URL} -o /tmp/yingsay_setup.sh && bash /tmp/yingsay_setup.sh; "
                  "echo; read -p '按回车关闭窗口…'")
        try:
            subprocess.Popen(self._wsl(script), creationflags=NEW_CONSOLE)
        except FileNotFoundError:
            return "没有找到 wsl.exe：请先在 PowerShell 运行 wsl --install"
        self.installed = None
        return "已打开安装窗口（约 10-20 分钟，主要是下载模型），完成后点「启动本地模型」"
