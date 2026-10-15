"""Beat-aware joint onset/offset snapping, with conservative fallbacks."""
from __future__ import annotations

import numpy as np


def snap_interval(start, end, bpm, subdivision, offset=0, beats=(), mode='strict', confidence=0):
    if mode == 'original' or (mode == 'gentle' and (len(beats) < 4 or confidence < .55)):
        return start, end
    subdivision = subdivision or 4
    if len(beats) >= 2:
        beats = np.asarray(beats, dtype=float) + offset
        # Detected beats are pulse locations, not inferred bar/downbeat positions.
        grid = np.concatenate([np.linspace(a, b, subdivision + 1)[:-1] for a, b in zip(beats[:-1], beats[1:])])
        grid = np.r_[grid, beats[-1]]
        def nearest(value):
            if value < beats[0] or value > beats[-1]:
                return value  # Do not extrapolate into untracked/free-time sections.
            return float(grid[np.argmin(abs(grid - value))])
    else:
        step = 60000 / bpm / subdivision
        def nearest(value):
            return max(0, round((value - offset) / step) * step + offset)
    a, b = nearest(start), nearest(end)
    if mode == 'gentle':
        # Small errors only; preserve swing, expressive timing and ornaments.
        tolerance = min(35, (end - start) * .15)
        a = a if abs(a - start) <= tolerance else start
        b = b if abs(b - end) <= tolerance else end
    if b <= a:
        return start, end
    return max(0, a), b
