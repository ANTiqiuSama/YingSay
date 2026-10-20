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
def make_sounds(folder: Path) -> dict[str, str]:
    import numpy as np

    from .audio import to_wav_bytes

    folder.mkdir(parents=True, exist_ok=True)
    spec = {"start": [(880, 0.06), (1320, 0.07)], "stop": [(1320, 0.05), (880, 0.07)],
            "error": [(330, 0.12), (250, 0.16)], "cancel": [(600, 0.06)]}
    paths = {}
    for name, tones in spec.items():
        p = folder / f"{name}.wav"
        if not p.exists():
            sr = 16000
            parts = []
            for f, d in tones:
                t = np.arange(int(sr * d)) / sr
                env = np.minimum(1, np.minimum(t, d - t) / 0.01)
                parts.append(0.22 * np.sin(2 * np.pi * f * t) * env)
            pcm = (np.concatenate(parts) * 32767).astype(np.int16)
            p.write_bytes(to_wav_bytes(pcm, sr))
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
