"""Bounded offline previews with incoming decays and sustained voices intact."""
from __future__ import annotations

import numpy as np

from ..audio import SAMPLE_RATE, SUSTAIN, Mixer


def render_window(score, start_ms, end_ms, instrument='Piano', melody_only=False, cancelled=lambda: False):
    if not 0 <= start_ms < end_ms:
        raise ValueError('试听范围无效')
    mixer = Mixer()
    mixer.prepare(instrument)
    mixer.volume = .42
    samples = mixer.cache[(instrument, 0)]
    tail = max(len(s[0]) for s in samples)/SAMPLE_RATE*1000
    warm = max(0, start_ms-tail)
    notes = [n for n in score.notes if n.time_ms < end_ms and
             (not melody_only or n.extra.get('role') != 'accompaniment')]
    if instrument in SUSTAIN:
        warm = min([warm]+[n.time_ms for n in notes if n.time_ms+n.duration_ms > start_ms])
    notes = [n for n in notes if n.time_ms >= warm]
    begin, finish = round(start_ms*SAMPLE_RATE/1000), round(end_ms*SAMPLE_RATE/1000)
    cursor = round(warm*SAMPLE_RATE/1000)
    output = np.zeros((finish-begin, 2), np.float32)

    def advance(target):
        nonlocal cursor
        while cursor < target:
            if cancelled():
                raise ValueError('试听准备已取消')
            count = min(8192, target-cursor)
            block = mixer.render(count)
            left, right = max(cursor, begin), min(cursor+count, finish)
            if right > left:
                output[left-begin:right-begin] = block[left-cursor:right-cursor]
            cursor += count

    for n in sorted(notes, key=lambda n: (n.time_ms, n.key)):
        advance(max(cursor, round(n.time_ms*SAMPLE_RATE/1000)))
        mixer.trigger((n.key,), (n.duration_ms,))
    advance(finish)
    return output
