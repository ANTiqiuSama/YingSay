"""Windows 平台相关：前台窗口、剪贴板、模拟按键粘贴、单实例、开机自启、提示音。"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)
IS_WIN = sys.platform == "win32"

if IS_WIN:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
    user32.EnumClipboardFormats.restype = wintypes.UINT
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
# 这些格式不是 HGLOBAL，无法按字节复制
_NON_HGLOBAL = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}


# ------------------------------------------------------------------ 进程 / 窗口
def set_dpi_aware() -> None:
    if not IS_WIN:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def foreground_info() -> tuple[int, str, str]:
    """返回 (hwnd, exe 名, 窗口标题)。"""
    if not IS_WIN:
        return 0, "", ""
    hwnd = user32.GetForegroundWindow() or 0
    if not hwnd:
        return 0, "", ""
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            size = wintypes.DWORD(1024)
            pbuf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                exe = os.path.basename(pbuf.value)
        finally:
            kernel32.CloseHandle(h)
    return int(hwnd), exe, buf.value


def focus_window(hwnd: int) -> None:
    if IS_WIN and hwnd and user32.GetForegroundWindow() != hwnd:
        try:
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.08)
        except Exception:
            pass


def work_area() -> tuple[int, int, int, int] | None:
    if not IS_WIN:
        return None
    rect = wintypes.RECT()
    if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
        return rect.left, rect.top, rect.right, rect.bottom
    return None


def make_overlay_window(hwnd: int) -> None:
    """不抢焦点、点击穿透、不出现在任务栏。"""
    if not IS_WIN or not hwnd:
        return
    GWL_EXSTYLE = -20
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= 0x08000000 | 0x00000080 | 0x00000020 | 0x00080000 | 0x00000008
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ctypes.c_long(style & 0xFFFFFFFF).value)


# ------------------------------------------------------------------ 剪贴板
@contextmanager
def _clipboard():
    for _ in range(40):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.025)
    else:
        raise OSError("无法打开剪贴板（被其他程序占用）")
    try:
        yield
    finally:
        user32.CloseClipboard()


def clipboard_seq() -> int:
    return int(user32.GetClipboardSequenceNumber()) if IS_WIN else 0


def get_clipboard_text() -> str | None:
    if not IS_WIN:
        return None
    with _clipboard():
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else None
        finally:
            kernel32.GlobalUnlock(h)


def _set_hglobal(fmt: int, data: bytes) -> None:
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(len(data), 1))
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    if not user32.SetClipboardData(fmt, h):
        kernel32.GlobalFree(h)


def set_clipboard_text(text: str, transient: bool = True) -> None:
    if not IS_WIN:
        return
    data = text.encode("utf-16-le") + b"\x00\x00"
    with _clipboard():
        user32.EmptyClipboard()
        _set_hglobal(CF_UNICODETEXT, data)
        if transient:  # 不进入 Win+V 剪贴板历史 / 云剪贴板
            zero = (0).to_bytes(4, "little")
            for name in ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"):
                fmt = user32.RegisterClipboardFormatW(name)
                if fmt:
                    _set_hglobal(fmt, zero)


def snapshot_clipboard(max_bytes: int = 64 * 1024 * 1024) -> list[tuple[int, bytes]] | None:
    """尽量完整地备份剪贴板（文本、图片 DIB、HTML、文件列表等 HGLOBAL 格式）。"""
    if not IS_WIN:
        return None
    items: list[tuple[int, bytes]] = []
    total = 0
    try:
        with _clipboard():
            fmt = 0
            while True:
                fmt = user32.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                if fmt in _NON_HGLOBAL:
                    continue
                h = user32.GetClipboardData(fmt)
                if not h:
                    continue
                size = kernel32.GlobalSize(h)
                if not size or total + size > max_bytes:
                    continue
                p = kernel32.GlobalLock(h)
                if not p:
                    continue
                try:
                    items.append((fmt, ctypes.string_at(p, size)))
                    total += size
                finally:
                    kernel32.GlobalUnlock(h)
    except Exception as e:
        log.warning("备份剪贴板失败: %s", e)
        return None
    return items


def restore_clipboard(items: list[tuple[int, bytes]] | None) -> None:
    if not IS_WIN or items is None:
        return
    try:
        with _clipboard():
            user32.EmptyClipboard()
            for fmt, data in items:
                try:
                    _set_hglobal(fmt, data)
                except Exception:
                    pass
    except Exception as e:
        log.warning("恢复剪贴板失败: %s", e)


# ------------------------------------------------------------------ 按键
_kb = None


def _controller():
    global _kb
    if _kb is None:
        from pynput.keyboard import Controller

        _kb = Controller()
    return _kb


def send_combo(*keys: str) -> None:
    from pynput.keyboard import Key

    kb = _controller()
    resolved = [getattr(Key, k) if len(k) > 1 else k for k in keys]
    for k in resolved:
        kb.press(k)
        time.sleep(0.01)
    for k in reversed(resolved):
        kb.release(k)
        time.sleep(0.01)


def wait_modifiers_released(timeout: float = 1.5) -> None:
    """等待用户松开 Ctrl/Alt/Shift/Win，避免与模拟的 Ctrl+V 叠加。"""
    if not IS_WIN:
        return
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    vks = (0x10, 0x11, 0x12, 0x5B, 0x5C)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not any(user32.GetAsyncKeyState(v) & 0x8000 for v in vks):
            return
        time.sleep(0.02)


def get_selected_text(timeout: float = 0.45) -> str | None:
    """通过模拟 Ctrl+C 读取当前选中的文字，读取后恢复剪贴板。没有选中则返回 None。"""
    if not IS_WIN:
        return None
    wait_modifiers_released()
    backup = snapshot_clipboard()
    seq = clipboard_seq()
    send_combo("ctrl", "c")
    end = time.monotonic() + timeout
    text = None
    while time.monotonic() < end:
        time.sleep(0.03)
        if clipboard_seq() != seq:
            time.sleep(0.03)
            try:
                text = get_clipboard_text()
            except Exception:
                text = None
            break
    if clipboard_seq() != seq:
        restore_clipboard(backup)
    return text if text and text.strip() else None


def paste_text(text: str, method: str = "ctrl_v", restore: bool = True) -> None:
    if not IS_WIN or not text:
        return
    wait_modifiers_released()
    if method == "type":
        _controller().type(text)
        return
    backup = snapshot_clipboard() if restore else None
    set_clipboard_text(text)
    time.sleep(0.03)
    if method == "shift_insert":
        send_combo("shift", "insert")
    else:
        send_combo("ctrl", "v")
    if restore and backup is not None:
        time.sleep(0.6)  # 给目标程序读取剪贴板的时间
        restore_clipboard(backup)


# ------------------------------------------------------------------ 单实例 / 自启
_mutex = None


def acquire_single_instance(name: str = "Local\\YingSay.SingleInstance") -> bool:
    global _mutex
    if not IS_WIN:
        return True
    _mutex = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" -m yingsay'


def set_autostart(enable: bool) -> None:
    if not IS_WIN:
        return
    import winreg

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                         winreg.KEY_SET_VALUE)
    try:
        try:  # 清理旧名字留下的自启项
            winreg.DeleteValue(key, "FreeTypeless")
        except FileNotFoundError:
            pass
        if enable:
            winreg.SetValueEx(key, "YingSay", 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, "YingSay")
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


# ------------------------------------------------------------------ 提示音
# 「应声」标志音：木琴/钢片琴质感的柔和音色（基频 + 少量泛音 + 指数衰减 + 轻混响），
# 音高取自 G 大调五声音阶，纯五度/大三度等协和音程，避免纯正弦的刺耳感。
SOUND_VERSION = "2"
_SR = 44100
_NOTE = {"G4": 392.00, "B4": 493.88, "D5": 587.33, "E5": 659.25, "G5": 783.99,
         "A5": 880.00, "B5": 987.77, "D6": 1174.66, "E4": 329.63, "C5": 523.25}

# 每个音：(音名, 起始秒, 力度, 衰减秒)
SOUND_SPEC = {
    "start": [("G5", 0.000, 0.85, 0.22), ("D6", 0.070, 1.00, 0.30)],               # 上行纯五度：「叮—咚↑」开始听
    "stop": [("D6", 0.000, 0.70, 0.18), ("G5", 0.060, 0.80, 0.30)],                # 下行回落：收音
    "done": [("G5", 0.000, 0.45, 0.20), ("B5", 0.045, 0.50, 0.22), ("D6", 0.090, 0.60, 0.35)],  # 大三和弦琶音：完成
    "error": [("E5", 0.000, 0.70, 0.20), ("C5", 0.110, 0.75, 0.32)],               # 柔和下行大三度：出错
    "cancel": [("B4", 0.000, 0.55, 0.12)],                                          # 单个闷音：取消
}


def _mallet(freq: float, dur: float, decay: float, np):
    t = np.arange(int(_SR * dur)) / _SR
    # 泛音：(倍频, 幅度, 衰减倍率) —— 4.0 与 2.76 给出类似木琴/钢片琴的明亮但不刺耳的音头
    partials = [(1.0, 1.0, 1.0), (2.0, 0.28, 0.55), (2.76, 0.10, 0.30), (4.0, 0.05, 0.22)]
    x = np.zeros_like(t)
    for mult, amp, dk in partials:
        f = freq * mult
        if f > 9000:
            continue
        x += amp * np.sin(2 * np.pi * f * t) * np.exp(-t / (decay * dk))
    attack = np.minimum(1.0, t / 0.004)          # 4ms 起音，去掉「咔」声
    return x * attack


def synth_sound(name: str):
    """生成标志音 PCM（int16, 44.1kHz 单声道）。"""
    import numpy as np

    notes = SOUND_SPEC[name]
    total = max(st + dk * 3 for _, st, _, dk in notes) + 0.12
    out = np.zeros(int(_SR * total))
    for note, st, vel, dk in notes:
        tone = _mallet(_NOTE[note], total - st, dk, np) * vel
        i = int(_SR * st)
        out[i:i + len(tone)] += tone[: len(out) - i]
    # 轻量混响：几路衰减回声，让声音更「圆」
    wet = np.zeros_like(out)
    for delay, g in ((0.023, 0.22), (0.041, 0.16), (0.067, 0.10), (0.093, 0.06)):
        d = int(_SR * delay)
        wet[d:] += out[:-d] * g
    out = out + wet
    # 一阶低通，柔化高频
    alpha = 0.55
    y = np.empty_like(out)
    acc = 0.0
    for i in range(0, len(out)):
        acc += alpha * (out[i] - acc)
        y[i] = acc
    fade = np.ones_like(y)
    n = int(_SR * 0.03)
    fade[-n:] = np.linspace(1, 0, n)
    y *= fade
    gain = {"done": 0.20, "cancel": 0.20, "stop": 0.24}.get(name, 0.30)
    y = y / (np.abs(y).max() + 1e-9) * gain          # 峰值约 -10~-14 dBFS，柔和不吓人
    return (y * 32767).astype(np.int16)


def make_sounds(folder: Path) -> dict[str, str]:
    from .audio import to_wav_bytes

    folder = folder / f"v{SOUND_VERSION}"
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in SOUND_SPEC:
        p = folder / f"{name}.wav"
        if not p.exists():
            p.write_bytes(to_wav_bytes(synth_sound(name), _SR))
        paths[name] = str(p)
    return paths


def play_sound(path: str | None) -> None:
    if not IS_WIN or not path:
        return
    try:
        import winsound

        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass
