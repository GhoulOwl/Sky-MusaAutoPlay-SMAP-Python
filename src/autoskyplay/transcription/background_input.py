"""Remove only a measured near-zero numerical tail from separated model input.

The original float mix is retained unchanged. This is deterministic preprocessing,
never a retry policy after a decoder failure. It cannot split an interior note.
"""

from __future__ import annotations

import numpy as np


def numerical_tail(audio, rate=16000):
    audio = np.asarray(audio)
    peak = float(np.max(np.abs(audio), initial=0))
    # Both an absolute and a relative floor protect quiet recordings. Keep an
    # extra 200 ms after the last significant frame, and require >=500 ms tail.
    rms_floor = min(10 ** (-90 / 20), peak * 10 ** (-70 / 20))
    peak_floor = min(10 ** (-70 / 20), peak * 10 ** (-50 / 20))
    hop = max(1, round(rate * 0.1))
    last = len(audio)
    for start in range(((len(audio) - 1) // hop) * hop, -1, -hop):
        block = audio[start : start + hop]
        if float(np.sqrt(np.mean(block**2))) > rms_floor or float(np.max(np.abs(block))) > peak_floor:
            break
        last = start
    keep = min(len(audio), last + round(0.2 * rate))
    if keep < rate or (len(audio) - keep) < rate * 0.5:
        keep = len(audio)
    return audio[:keep], dict(
        policy="near-zero-tail-v1",
        original_samples=len(audio),
        model_samples=keep,
        rms_floor=rms_floor,
        peak_floor=peak_floor,
        scale=1,
        omitted_tail_ms=(len(audio) - keep) / rate * 1000,
    )
