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
