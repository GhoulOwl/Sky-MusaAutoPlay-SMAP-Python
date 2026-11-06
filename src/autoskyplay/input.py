"""Windows game input. Imported on every platform, activated only explicitly."""

from __future__ import annotations

import ctypes
import ntpath
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


class _WindowsKeyOutput:
    def __init__(self, keys: list[str], hold_ms: int = 25, sustain: bool = False):
        if sys.platform != "win32":
            raise RuntimeError("游戏演奏目前仅支持 Windows；当前平台可以试听、编辑和练习")
        validate_keys(keys)
        self.vks = [virtual_key(k) for k in keys]
        self.hold_ms = max(10, min(500, hold_ms))
        self.sustain = sustain
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.pending: dict[int, float] = {}

    def _send(self, keys: list[int], up: bool):
        raise NotImplementedError

    def trigger(self, keys: tuple[int, ...], durations: tuple[float, ...]):
        keys = tuple(dict.fromkeys(keys))
        repeated = [k for k in keys if k in self.pending]
        self._send(repeated, True)
        # Track before sending so a partial failure is cleaned up too.
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


class WindowsOutput(_WindowsKeyOutput):
    def __init__(self, keys: list[str], hold_ms: int = 25, sustain: bool = False):
        super().__init__(keys, hold_ms, sustain)
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self.user32.MapVirtualKeyW.restype = wintypes.UINT

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


def _window_owner(user32, hwnd: int) -> tuple[int, int]:
    pid = wintypes.DWORD()
    thread_id = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return thread_id, pid.value


def _find_sky_window(user32) -> tuple[int, tuple[int, int]]:
    user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowExW.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    hwnd = None
    target = None
    inaccessible = False
    path = ctypes.create_unicode_buffer(32768)
    while hwnd := user32.FindWindowExW(None, hwnd, "TgcMainWindow", None):
        if not user32.IsWindowVisible(hwnd):
            continue
        owner = _window_owner(user32, hwnd)
        if not owner[0]:
            continue
        process = kernel32.OpenProcess(0x1000, False, owner[1])  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            inaccessible = True
            continue
        try:
            length = wintypes.DWORD(len(path))
            found = kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(length))
        finally:
            kernel32.CloseHandle(process)
        if not found:
            inaccessible = True
        elif ntpath.basename(path.value).casefold() == "sky.exe":
            if target is not None:
                raise RuntimeError("检测到多个光·遇窗口。请只保留一个游戏窗口后重试")
            target = hwnd, owner
    if target is not None:
        return target
    if inaccessible:
        raise RuntimeError("无法验证光·遇窗口。请确认游戏和 AutoSkyPlay 以相同的权限运行")
    raise RuntimeError("未找到光·遇窗口。请打开 Windows 版光·遇并拿出乐器后重试")


WM_ACTIVATE, WA_ACTIVE = 0x0006, 1
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
# US Set 1 scan codes in physical row order, as used by the enhanced mode.
US_SCAN_CODES = {
    **dict(zip("1234567890-=", range(0x02, 0x0E), strict=True)),
    **dict(zip("QWERTYUIOP[]", range(0x10, 0x1C), strict=True)),
    **dict(zip("ASDFGHJKL;'`", range(0x1E, 0x2A), strict=True)),
    **dict(zip("\\ZXCVBNM,./", range(0x2B, 0x36), strict=True)),
    "SPACE": 0x39,
}


class WindowsBackgroundOutput(_WindowsKeyOutput):
    def __init__(self, keys: list[str], hold_ms: int = 25, sustain: bool = False):
        super().__init__(keys, hold_ms, sustain)
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.hwnd, self.owner = _find_sky_window(self.user32)
        self.key_params = [1 | (US_SCAN_CODES[key.upper()] << 16) for key in keys]
        self.next_window_check = 0.0
        self._check_window()

    def _check_window(self):
        if _window_owner(self.user32, self.hwnd) != self.owner:
            raise RuntimeError("光·遇窗口已关闭或变更。请重新开始后台演奏")

    def _post(self, message: int, wparam: int, lparam: int):
        if not self.user32.PostMessageW(self.hwnd, message, wparam, lparam):
            error = ctypes.get_last_error()
            if error == 5:  # ERROR_ACCESS_DENIED (including UIPI)
                raise RuntimeError("无法向光·遇发送消息。请确认游戏和 AutoSkyPlay 以相同的权限运行")
            raise RuntimeError(f"后台演奏发送消息失败（PostMessageW，Windows 错误 {error}）")

    def _send(self, keys: list[int], up: bool):
        if not keys:
            return
        if up:
            self._check_window()
            error = None
            for key in keys:
                try:
                    self._post(WM_KEYUP, self.vks[key], self.key_params[key] | 0xC0000000)
                except RuntimeError as exc:
                    error = error or exc
                else:
                    self.pending.pop(key, None)
            if error is not None:
                raise error
        else:
            posted = 0
            try:
                self._check_window()
                # SkyMusicPlay Lite's enhanced mode posts this before each chord.
                self._post(WM_ACTIVATE, WA_ACTIVE, 0)
                for key in keys:
                    self._post(WM_KEYDOWN, self.vks[key], self.key_params[key])
                    posted += 1
            finally:
                for key in keys[posted:]:
                    self.pending.pop(key, None)

    def pump(self):
        now = time.monotonic()
        if now >= self.next_window_check:
            self._check_window()
            self.next_window_check = now + 0.25
        super().pump()


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
