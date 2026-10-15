"""Windows scan-code input. Imported on every platform, activated only explicitly."""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

DEFAULT_KEYS = ["Y", "U", "I", "O", "P", "H", "J", "K", "L", ";", "N", "M", ",", ".", "/"]
PUNCTUATION_VK = {
    ";": 0xBA,
    "=": 0xBB,
    ",": 0xBC,
    "-": 0xBD,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "[": 0xDB,
    "\\": 0xDC,
    "]": 0xDD,
    "'": 0xDE,
    "SPACE": 0x20,
}
INJECTED_MARKER = 0x534D4150


def virtual_key(label: str) -> int:
    label = label.upper()
    if len(label) == 1 and label.isascii() and label.isalnum():
        return ord(label)
    if label in PUNCTUATION_VK:
        return PUNCTUATION_VK[label]
    raise ValueError(f"不支持的琴键: {label}（请使用字母、数字、标点或 SPACE）")


def validate_keys(keys: list[str]):
    if len(keys) != 15 or len({virtual_key(k) for k in keys}) != 15:
        raise ValueError("需要 15 个互不重复的键位")


# Explicit Windows widths keep layout testable on non-Windows development hosts.
U32, I32, U16 = ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint16
ULONG_PTR = ctypes.c_size_t


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", I32),
        ("dy", I32),
        ("mouseData", U32),
        ("dwFlags", U32),
        ("time", U32),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", U16), ("wScan", U16), ("dwFlags", U32), ("time", U32), ("dwExtraInfo", ULONG_PTR)]


class InputUnion(ctypes.Union):
    _fields_ = [("mi", MouseInput), ("ki", KeyboardInput)]


class Input(ctypes.Structure):
    _fields_ = [("type", U32), ("u", InputUnion)]


class WindowsOutput:
    def __init__(self, keys: list[str], hold_ms: int = 25, sustain: bool = False):
        if sys.platform != "win32":
            raise RuntimeError("游戏演奏目前仅支持 Windows；当前平台可以试听、编辑和练习")
        validate_keys(keys)
        self.vks = [virtual_key(k) for k in keys]
        self.hold_ms = max(10, min(500, hold_ms))
        self.sustain = sustain
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self.user32.MapVirtualKeyW.restype = wintypes.UINT
        self.pending: dict[int, float] = {}

    def _send(self, keys: list[int], up: bool):
        if not keys:
            return
        items = []
        for key in keys:
            scan = self.user32.MapVirtualKeyW(self.vks[key], 0)
            if not scan:
                raise RuntimeError(f"无法映射按键: {self.vks[key]}")
            items.append(
                Input(1, InputUnion(ki=KeyboardInput(0, scan, 8 | (2 if up else 0), 0, INJECTED_MARKER)))
            )
        values = (Input * len(items))(*items)
        if self.user32.SendInput(len(values), values, ctypes.sizeof(Input)) != len(values):
            raise RuntimeError("SendInput 失败。若游戏以管理员身份运行，请同样以管理员身份启动 AutoSkyPlay")

    def trigger(self, keys: tuple[int, ...], durations: tuple[float, ...]):
        keys = tuple(dict.fromkeys(keys))
        repeated = [k for k in keys if k in self.pending]
        self._send(repeated, True)
        # Track before injection so a partial SendInput failure is cleaned up too.
        now = time.monotonic()
        for i, key in enumerate(keys):
            duration = durations[i] if i < len(durations) else self.hold_ms
            hold = max(10, min(600000, duration)) if getattr(self, 'sustain', False) else self.hold_ms
            self.pending[key] = now + hold / 1000
        self._send(list(keys), False)

    def restore(self, keys: tuple[int, ...], durations: tuple[float, ...]):
        if self.sustain:
            self.trigger(keys, durations)

    def pump(self):
        expired = [k for k, deadline in self.pending.items() if deadline <= time.monotonic()]
        self._send(expired, True)
        for key in expired:
            self.pending.pop(key, None)

    def silence(self):
        self._send(list(self.pending), True)
        self.pending.clear()


class GlobalKeyboard:
    """Only Windows needs a global listener; callbacks must marshal to the Qt thread."""

    def __init__(self, on_key):
        self.on_key = on_key
        self.listener = None
        self.down = set()

    def start(self):
        if sys.platform != "win32":
            raise RuntimeError("全局热键和游戏练习监听仅在 Windows 启用")
        from pynput import keyboard

        def label(key):
            if getattr(key, "char", None):
                return key.char.upper()
            return {
                keyboard.Key.space: "SPACE",
                keyboard.Key.left: "LEFT",
                keyboard.Key.right: "RIGHT",
                keyboard.Key.esc: "ESC",
                **{getattr(keyboard.Key, f"f{i}"): f"F{i}" for i in range(1, 7)},
            }.get(key)

        def press(key):
            name = label(key)
            if name and name not in self.down:
                self.down.add(name)
                self.on_key(name, True)

        def release(key):
            name = label(key)
            if name:
                self.down.discard(name)
                self.on_key(name, False)

        def event_filter(msg, data):
            # False skips our generated input without suppressing it in the game.
            return data.dwExtraInfo != INJECTED_MARKER

        self.listener = keyboard.Listener(
            on_press=press, on_release=release, win32_event_filter=event_filter, suppress=False
        )
        self.listener.start()

    def stop(self):
        if self.listener:
            self.listener.stop()
            self.listener = None
        self.down.clear()
