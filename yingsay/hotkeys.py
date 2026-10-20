"""全局热键：按住说话 / 轻点切换免提 / Esc 取消。

状态机与 pynput 解耦，便于测试。热键写法示例：
  "ctrl_r"（右 Ctrl）、"f8"、"alt_r"、"ctrl+shift+space"、"caps_lock"、"cmd_r"
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

FRIENDLY = {
    "right_ctrl": "ctrl_r", "rctrl": "ctrl_r", "ctrlright": "ctrl_r", "right_control": "ctrl_r",
    "left_ctrl": "ctrl_l", "lctrl": "ctrl_l", "control": "ctrl",
    "right_alt": "alt_r", "ralt": "alt_r", "altgr": "alt_gr", "left_alt": "alt_l",
    "right_shift": "shift_r", "rshift": "shift_r", "left_shift": "shift_l",
    "win": "cmd", "windows": "cmd", "super": "cmd", "right_win": "cmd_r", "left_win": "cmd_l",
    "capslock": "caps_lock", "caps": "caps_lock", "escape": "esc", "return": "enter",
    "scrolllock": "scroll_lock", "pause_break": "pause", "insert": "insert", "menu": "menu", "apps": "menu",
}

# 一个物理按键能满足哪些名字
EXPAND = {
    "ctrl": {"ctrl", "ctrl_l"}, "ctrl_l": {"ctrl", "ctrl_l"}, "ctrl_r": {"ctrl", "ctrl_r"},
    "shift": {"shift", "shift_l"}, "shift_l": {"shift", "shift_l"}, "shift_r": {"shift", "shift_r"},
    "alt": {"alt", "alt_l"}, "alt_l": {"alt", "alt_l"}, "alt_r": {"alt", "alt_r", "alt_gr"},
    "alt_gr": {"alt", "alt_r", "alt_gr"},
    "cmd": {"cmd", "cmd_l"}, "cmd_l": {"cmd", "cmd_l"}, "cmd_r": {"cmd", "cmd_r"},
}


def normalize(name: str) -> str:
    n = name.strip().lower().replace(" ", "_").replace("-", "_")
    return FRIENDLY.get(n, n)


def parse_hotkey(spec: str) -> frozenset[str]:
    if not spec or not spec.strip():
        return frozenset()
    return frozenset(normalize(p) for p in spec.split("+") if p.strip())


def format_hotkey(keys) -> str:
    order = ["ctrl", "ctrl_l", "ctrl_r", "alt", "alt_l", "alt_r", "alt_gr", "shift", "shift_l", "shift_r",
             "cmd", "cmd_l", "cmd_r"]
    ks = sorted(keys, key=lambda k: (order.index(k) if k in order else 100, k))
    return "+".join(ks)


def expand(name: str) -> set[str]:
    return EXPAND.get(name, {name})


class HotkeyStateMachine:
    IDLE, HOLD, HANDSFREE = "idle", "hold", "handsfree"

    def __init__(self, bindings: dict[str, str], on_start: Callable[[str], None],
                 on_stop: Callable[[str], None], on_cancel: Callable[[str], None],
                 on_mode_change: Callable[[str], None] | None = None,
                 on_handsfree: Callable[[str], None] | None = None,
                 tap_to_toggle: bool = True, tap_threshold: float = 0.3,
                 cancel_on_other_key: bool = True, clock=time.monotonic):
        self.on_start, self.on_stop, self.on_cancel = on_start, on_stop, on_cancel
        self.on_mode_change = on_mode_change or (lambda m: None)
        self.on_handsfree = on_handsfree or (lambda m: None)
        self.tap_to_toggle = tap_to_toggle
        self.tap_threshold = tap_threshold
        self.cancel_on_other_key = cancel_on_other_key
        self.clock = clock
        self.pressed: set[str] = set()
        self.state = self.IDLE
        self.mode: str | None = None
        self.t0 = 0.0
        self.capture_cb: Callable[[str], None] | None = None
        self._capture_keys: set[str] = set()
        self.set_bindings(bindings)

    def set_bindings(self, bindings: dict[str, str]) -> None:
        self.bindings = {m: parse_hotkey(s) for m, s in bindings.items() if parse_hotkey(s)}

    def _satisfied(self) -> set[str]:
        s: set[str] = set()
        for p in self.pressed:
            s |= expand(p)
        return s

    def _best_match(self, trigger: str | None = None) -> str | None:
        sat = self._satisfied()
        best, best_len = None, 0
        for mode, keys in self.bindings.items():
            if keys <= sat and len(keys) > best_len:
                if trigger is None or (expand(trigger) & keys):
                    best, best_len = mode, len(keys)
        return best

    def reset(self) -> None:
        self.state, self.mode = self.IDLE, None

    # ------------------------------------------------------------------
    def press(self, name: str) -> None:
        if name in self.pressed:  # 长按自动重复
            return
        self.pressed.add(name)

        if self.capture_cb is not None:
            self._capture_keys.add(name)
            return

        if name == "esc" and self.state != self.IDLE:
            mode = self.mode
            self.reset()
            self.on_cancel(mode or "")
            return

        if self.state == self.IDLE:
            m = self._best_match(name)
            if m:
                self.state, self.mode, self.t0 = self.HOLD, m, self.clock()
                self.on_start(m)
        elif self.state == self.HOLD:
            m = self._best_match(name)
            cur = self.bindings.get(self.mode, frozenset())
            if m and m != self.mode and cur < self.bindings[m]:
                self.mode = m
                self.on_mode_change(m)
            elif not (expand(name) & cur) and self.cancel_on_other_key and self.clock() - self.t0 < 1.0:
                # 用户其实是在按组合键（例如 右Ctrl+C），不是要说话
                mode = self.mode
                self.reset()
                self.on_cancel(mode or "")
        elif self.state == self.HANDSFREE:
            keys = self.bindings.get(self.mode, frozenset())
            if keys and keys <= self._satisfied() and (expand(name) & keys):
                mode = self.mode
                self.reset()
                self.on_stop(mode or "dictate")

    def release(self, name: str) -> None:
        self.pressed.discard(name)
        if self.capture_cb is not None:
            if not self.pressed and self._capture_keys:
                cb, keys = self.capture_cb, set(self._capture_keys)
                self.capture_cb, self._capture_keys = None, set()
                cb(format_hotkey(keys))
            return
        if self.state == self.HOLD:
            keys = self.bindings.get(self.mode, frozenset())
            if not keys <= self._satisfied():
                dur = self.clock() - self.t0
                if self.tap_to_toggle and dur < self.tap_threshold:
                    self.state = self.HANDSFREE
                    self.on_handsfree(self.mode or "dictate")
                else:
                    mode = self.mode
                    self.reset()
                    self.on_stop(mode or "dictate")

    def capture_next(self, cb: Callable[[str], None]) -> None:
        self.pressed.clear()
        self._capture_keys = set()
        self.capture_cb = cb


def key_name(key) -> str | None:
    """把 pynput 的 Key/KeyCode 转为字符串名。"""
    from pynput import keyboard

    if isinstance(key, keyboard.Key):
        return key.name
    vk = getattr(key, "vk", None)
    if vk is not None:
        if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
            return chr(vk).lower()
        if 0x70 <= vk <= 0x87:
            return f"f{vk - 0x6F}"
    ch = getattr(key, "char", None)
    if ch:
        return ch.lower()
    if vk is not None:
        return f"vk{vk}"
    return None


class GlobalHotkeys:
    """pynput 低级键盘钩子 → 状态机。回调在独立线程执行，保证钩子本身极快返回。"""

    def __init__(self, machine: HotkeyStateMachine):
        self.machine = machine
        self._listener = None
        self._lock = threading.Lock()

    def start(self) -> None:
        from pynput import keyboard

        def flt(msg, data):
            # 忽略程序注入的按键（包括本程序自己发送的 Ctrl+V / Ctrl+C）
            return not (getattr(data, "flags", 0) & 0x10)

        def on_press(key):
            n = key_name(key)
            if n:
                with self._lock:
                    try:
                        self.machine.press(n)
                    except Exception:
                        log.exception("热键处理出错")

        def on_release(key):
            n = key_name(key)
            if n:
                with self._lock:
                    try:
                        self.machine.release(n)
                    except Exception:
                        log.exception("热键处理出错")

        kwargs = {"on_press": on_press, "on_release": on_release}
        import sys

        if sys.platform == "win32":
            kwargs["win32_event_filter"] = flt
        self._listener = keyboard.Listener(**kwargs)
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None

    def clear_pressed(self) -> None:
        with self._lock:
            self.machine.pressed.clear()
