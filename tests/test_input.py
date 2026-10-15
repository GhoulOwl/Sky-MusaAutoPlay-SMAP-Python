import ctypes

import pytest

from autoskyplay.input import DEFAULT_KEYS, Input, WindowsOutput, validate_keys


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
