import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from autoskyplay import input as game_input
from autoskyplay.input import DEFAULT_KEYS, Input, WindowsBackgroundOutput, WindowsOutput, validate_keys


def test_windows_input_structure_size():
    assert ctypes.sizeof(Input) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_duplicate_bindings_rejected():
    keys = list(DEFAULT_KEYS)
    keys[1] = keys[0]
    with pytest.raises(ValueError):
        validate_keys(keys)


def test_scan_code_chord_is_sent_together_and_released():
    class User32:
        def __init__(self):
            self.calls = []

        def MapVirtualKeyW(self, vk, kind):
            return vk - 20

        def SendInput(self, count, inputs, size):
            self.calls.append([(item.u.ki.wVk, item.u.ki.wScan, item.u.ki.dwFlags) for item in inputs])
            return count

    output = WindowsOutput.__new__(WindowsOutput)
    output.user32 = User32()
    output.vks = [89, 85, 73, 79, 80, 72, 74, 75, 76, 186, 78, 77, 188, 190, 191]
    output.pending, output.hold_ms = {}, 25
    output.trigger((0, 4, 7), (100, 100, 100))
    assert len(output.user32.calls) == 1
    assert len(output.user32.calls[0]) == 3
    assert all(vk == 0 and flags == 8 for vk, scan, flags in output.user32.calls[0])
    output.silence()
    assert all(flags == 10 for vk, scan, flags in output.user32.calls[1])
    assert not output.pending


def test_duration_mode_releases_and_retriggers_without_stuck_keys(monkeypatch):
    now = [10.]
    monkeypatch.setattr('autoskyplay.input.time.monotonic', lambda: now[0])
    output = WindowsOutput.__new__(WindowsOutput)
    output.pending, output.hold_ms, output.sustain = {}, 25, True
    calls = []
    output._send = lambda keys, up: calls.append((list(keys), up))
    output.trigger((0, 4), (800, 200))
    assert output.pending == {0: 10.8, 4: 10.2}
    now[0] = 10.3
    output.pump()
    assert calls[-1] == ([4], True) and 0 in output.pending
    output.trigger((0,), (100,))
    assert calls[-2:] == [([0], True), ([0], False)]
    now[0] = 10.5
    output.pump()
    assert not output.pending


def test_seek_restore_requires_explicit_game_sustain_mode(monkeypatch):
    monkeypatch.setattr('autoskyplay.input.time.monotonic', lambda: 10.)
    output = WindowsOutput.__new__(WindowsOutput)
    output.pending, output.hold_ms, output.sustain = {}, 25, False
    calls = []
    output._send = lambda keys, up: calls.append((list(keys), up))
    output.restore((0,), (750,))
    assert not calls and not output.pending
    output.sustain = True
    output.restore((0,), (750,))
    assert output.pending == {0: 10.75}


@pytest.fixture
def background_win32(monkeypatch):
    owners = {11: (100, 101), 22: (200, 202)}
    process_paths = {101: r"C:\Other\app.exe", 202: r"C:\Games\Sky.exe"}

    def window_owner(hwnd, pid):
        thread, process = owners.get(hwnd, (0, 0))
        ctypes.cast(pid, ctypes.POINTER(game_input.wintypes.DWORD)).contents.value = process
        return thread

    def query_image(process, flags, buffer, size):
        buffer.value = process_paths[process]
        return True

    user32 = SimpleNamespace(
        FindWindowExW=Mock(side_effect=lambda parent, after, cls, title: {None: 11, 11: 22, 22: None}[after]),
        IsWindowVisible=Mock(return_value=True),
        GetWindowThreadProcessId=Mock(side_effect=window_owner),
        PostMessageW=Mock(return_value=True),
    )
    kernel32 = SimpleNamespace(
        OpenProcess=Mock(side_effect=lambda access, inherit, pid: pid),
        QueryFullProcessImageNameW=Mock(side_effect=query_image),
        CloseHandle=Mock(),
    )
    libraries = {"user32": user32, "kernel32": kernel32}
    monkeypatch.setattr(game_input, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: libraries[name], raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    return user32, kernel32, owners


def test_background_chord_and_release_are_posted_only_to_sky(background_win32, monkeypatch):
    user32, kernel32, _ = background_win32
    now = [10.0]
    monkeypatch.setattr(game_input.time, "monotonic", lambda: now[0])
    output = WindowsBackgroundOutput(DEFAULT_KEYS, sustain=True)
    output.trigger((0, 14), (800, 200))
    y, slash = 0x00150001, 0x00350001
    now[0] = 10.3
    output.pump()
    output.silence()
    assert [call.args for call in user32.PostMessageW.call_args_list] == [
        (22, 0x0006, 1, 0),
        (22, 0x0100, ord("Y"), y),
        (22, 0x0100, 0xBF, slash),
        (22, 0x0101, 0xBF, slash | 0xC0000000),
        (22, 0x0101, ord("Y"), y | 0xC0000000),
    ]
    assert [call.args for call in kernel32.CloseHandle.call_args_list] == [(101,), (202,)]
    assert not output.pending


def test_background_releases_only_notes_that_were_posted_on_partial_failure(background_win32):
    user32, _, _ = background_win32
    output = WindowsBackgroundOutput(DEFAULT_KEYS)
    user32.PostMessageW.side_effect = [True, True, False, True]
    with pytest.raises(RuntimeError, match="相同的权限"):
        output.trigger((0, 4, 7), ())
    assert list(output.pending) == [0]
    output.silence()
    assert user32.PostMessageW.call_args.args[:3] == (22, 0x0101, ord("Y"))
    assert not output.pending


def test_background_retries_only_failed_releases(background_win32):
    user32, _, _ = background_win32
    output = WindowsBackgroundOutput(DEFAULT_KEYS)
    output.trigger((0, 4), ())
    user32.PostMessageW.side_effect = [False, True, True]
    with pytest.raises(RuntimeError, match="相同的权限"):
        output.silence()
    assert list(output.pending) == [0]
    output.silence()
    assert [call.args[2] for call in user32.PostMessageW.call_args_list[-3:]] == [ord("Y"), ord("P"), ord("Y")]
    assert not output.pending


def test_background_stops_if_the_sky_window_is_replaced(background_win32):
    user32, _, owners = background_win32
    output = WindowsBackgroundOutput(DEFAULT_KEYS)
    owners[22] = (300, 303)
    with pytest.raises(RuntimeError, match="窗口已关闭或变更"):
        output.pump()
    user32.PostMessageW.assert_not_called()


def test_background_requires_a_verified_sky_window(background_win32):
    user32, _, owners = background_win32
    del owners[22]
    with pytest.raises(RuntimeError, match="未找到光·遇窗口"):
        WindowsBackgroundOutput(DEFAULT_KEYS)
    user32.PostMessageW.assert_not_called()
