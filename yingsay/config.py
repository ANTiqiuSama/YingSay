"""配置读写。配置文件位于 %APPDATA%\\YingSay\\config.json。

API Key 在 Windows 上使用 DPAPI（当前用户）加密后保存，不以明文落盘。
环境变量 MIMO_API_KEY / MIMO_BASE_URL 可覆盖配置文件。
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

TOKEN_PLAN_CLUSTERS = {
    "中国": "https://token-plan-cn.xiaomimimo.com/v1",
    "新加坡": "https://token-plan-sgp.xiaomimimo.com/v1",
    "欧洲": "https://token-plan-ams.xiaomimimo.com/v1",
}
TOKEN_PLAN_BASE_URL = TOKEN_PLAN_CLUSTERS["中国"]
PAYG_BASE_URL = "https://api.xiaomimimo.com/v1"

DEFAULTS: dict = {
    # ---- 接口 ----
    "api_key": "",
    "base_url": TOKEN_PLAN_BASE_URL,
    # asr+llm: 专用 ASR 识别 + LLM 润色（默认，最准）
    # asr_only: 只用 ASR（最快、最省）
    # omni: 全模态模型一步完成“听写+润色”
    "engine": "asr+llm",
    "asr_model": "mimo-v2.5-asr",
    "asr_language": "auto",  # auto / zh / en
    "llm_model": "mimo-v2.6-flash",
    "command_model": "mimo-v2.6-flash",
    "omni_model": "mimo-v2.6-flash",
    "llm_thinking": False,
    "request_timeout": 60,
    # ---- 速度 ----
    "upload_format": "mp3",  # mp3（体积约为 wav 的 1/8，上传更快）/ wav
    "live_segmenting": True,  # 边说边识别：在停顿处切段并提前上传
    "polish_skip_chars": 6,  # 不超过这么多字且没有口头禅时跳过 AI 润色
    "warmup": True,  # 按下热键时预热 HTTPS 连接
    "hedge_requests": True,  # 对冲请求：慢请求超时前再发一份，谁先回来用谁（少量额外用量换稳定速度）
    "hedge_scale": 1.0,  # 对冲等待时间倍数，网络稳定可调大
    "plan": "Max",
    # ---- 本地模型（WSL2 + NVIDIA 显卡）----
    "asr_backend": "auto",  # auto=本地服务在就用本地，否则云端 / local / mimo
    "llm_backend": "auto",  # 润色：auto / local / mimo（指令/提问在 auto 下仍走云端）
    "local_asr_model": "qwen3-asr-1.7b",  # qwen3-asr-1.7b / qwen3-asr-0.6b / sensevoice
    "local_llm_model": "Qwen/Qwen3-4B-Instruct-2507",  # 或 Qwen/Qwen3-8B-FP8 / 关闭
    "local_autostart": True,  # 启动应声时自动在 WSL 里拉起本地模型
    "local_stop_on_exit": False,  # 退出应声时停止本地模型（释放显存；下次需重新加载）
    "local_wsl_distro": "",  # 留空=默认发行版
    "local_host": "127.0.0.1",  # Token Plan 套餐（用于显示月额度占比）：Lite / Standard / Pro / Max
    # ---- 热键 ----
    "hotkey_dictate": "ctrl_r",
    "hotkey_command": "f8",
    "hotkey_translate": "f9",
    "tap_to_toggle": True,
    "tap_threshold_ms": 300,
    "cancel_on_other_key": True,
    # ---- 录音 ----
    "input_device": "",
    "max_record_seconds": 600,
    "min_record_ms": 350,
    # ---- 输出 ----
    "paste_method": "ctrl_v",  # ctrl_v / shift_insert / type
    "restore_clipboard": True,
    "output_script": "auto",  # auto / simplified / traditional
    "translate_target": "English",
    # ---- 个性化 ----
    "dictionary": [],
    "replacements": [],  # [["错词", "正词"], ...]
    "custom_instructions": "",
    "app_styles": {},  # {"wechat.exe": "口语化，不加句号"}
    # ---- 界面 ----
    "sounds": True,
    "show_overlay": True,
    "autostart": False,
    "keep_recordings": 20,
    "history_limit": 2000,
}


def app_dir() -> Path:
    override = os.environ.get("YINGSAY_HOME")
    if override:
        p = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
        p = base / "YingSay"
        _migrate_legacy(base / "FreeTypeless", p)
    else:
        p = Path.home() / ".yingsay"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _migrate_legacy(old: Path, new: Path) -> None:
    """从旧名字 FreeTypeless 迁移配置与历史（只迁移一次）。"""
    if new.exists() or not old.exists():
        return
    import shutil

    try:
        new.mkdir(parents=True, exist_ok=True)
        for name in ("config.json", "history.jsonl"):
            if (old / name).exists():
                shutil.copy2(old / name, new / name)
    except Exception as e:  # pragma: no cover
        log.warning("迁移旧配置失败: %s", e)


# ---------------------------------------------------------------- DPAPI
def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    fn.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    fn.restype = wintypes.BOOL
    buf = ctypes.create_string_buffer(data, len(data))
    inp = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = DATA_BLOB()
    if not fn(ctypes.byref(inp), None, None, None, None, 0x01, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def encrypt_secret(s: str) -> dict:
    if not s:
        return {"api_key": ""}
    if sys.platform == "win32":
        try:
            return {"api_key": "", "api_key_dpapi": base64.b64encode(_dpapi(s.encode("utf-8"), True)).decode()}
        except Exception as e:  # pragma: no cover
            log.warning("DPAPI 加密失败，退回明文保存: %s", e)
    return {"api_key": s}


def decrypt_secret(raw: dict) -> str:
    enc = raw.get("api_key_dpapi")
    if enc and sys.platform == "win32":
        try:
            return _dpapi(base64.b64decode(enc), False).decode("utf-8")
        except Exception as e:  # pragma: no cover
            log.warning("DPAPI 解密失败: %s", e)
    return raw.get("api_key", "") or ""


# ---------------------------------------------------------------- Config
class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or (app_dir() / "config.json")
        self._lock = threading.RLock()
        self.data: dict = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        with self._lock:
            self.data = copy.deepcopy(DEFAULTS)
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
                    key = decrypt_secret(raw)
                    raw.pop("api_key_dpapi", None)
                    self.data.update({k: v for k, v in raw.items() if k in DEFAULTS})
                    self.data["api_key"] = key
                except Exception as e:
                    log.error("读取配置失败，使用默认配置: %s", e)

    def save(self) -> None:
        with self._lock:
            out = copy.deepcopy(self.data)
            out.pop("api_key", None)
            out.update(encrypt_secret(self.data.get("api_key", "")))
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def get(self, key: str, default=None):
        with self._lock:
            if key == "api_key" and os.environ.get("MIMO_API_KEY"):
                return os.environ["MIMO_API_KEY"].strip()
            if key == "base_url" and os.environ.get("MIMO_BASE_URL"):
                return os.environ["MIMO_BASE_URL"].strip()
            return self.data.get(key, DEFAULTS.get(key, default))

    def __getitem__(self, key: str):
        return self.get(key)

    def update(self, values: dict) -> None:
        with self._lock:
            for k, v in values.items():
                if k in DEFAULTS:
                    self.data[k] = v
            self.save()
