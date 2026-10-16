"""Lossless MIDI note timing for local sources; no audio model is involved."""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from ..midi import MidiImporter
from .melody import MelodyNote


@dataclass
class MidiResult:
    raw_notes: list[MelodyNote]
    duration_ms: float
    bpm: float
    metadata: dict = field(default_factory=dict)

    def notes(self, threshold=.35, minimum_ms=80):
        return list(self.raw_notes)


def read_midi(path, tracks=None, start=0, length=600):
    path = Path(path)
    if path.stat().st_size > 10 * 1024**2:
        raise ValueError('请选择不超过 10 MB 的 MIDI 文件')
    importer = MidiImporter(path)
    selected = set(tracks) if tracks is not None else set(range(len(importer.events)))
    if not selected or any(i < 0 or i >= len(importer.events) for i in selected):
        raise ValueError('请选择有效 MIDI 音轨')
    all_notes = []
    for i in sorted(selected):
        active = defaultdict(deque)
        last = 0
        for tick, message in importer.events[i]:
            last = importer.tick_ms(tick)
            if message.type not in ('note_on', 'note_off') or message.channel == 9:
                continue
            key = (message.channel, message.note)
            if message.type == 'note_on' and message.velocity:
                active[key].append(last)
            elif active[key]:
                begin = active[key].popleft()
                all_notes.append(MelodyNote(message.note, begin, max(1, last - begin), 1, source=f"midi:{i + 1}"))
        for (_, pitch), begins in active.items():
            for begin in begins:
                all_notes.append(MelodyNote(pitch, begin, max(20, last - begin), 1, source=f"midi:{i + 1}"))
    duration = max(max((n.start_ms + n.duration_ms for n in all_notes), default=0),
                   max((importer.tick_ms(events[-1][0]) for events in importer.events if events), default=0))
    if not all_notes or not 0 < duration <= 86400000 or len(all_notes) > 100000:
        raise ValueError('MIDI 没有有效旋律，或音符数量/时长超出限制')
    low, high = start * 1000, min(duration, (start + length) * 1000)
    if high <= low:
        raise ValueError('所选范围没有 MIDI 音符')
    notes = []
    for note in all_notes:
        begin, end = max(low, note.start_ms), min(high, note.start_ms + note.duration_ms)
        if end > begin:
            notes.append(MelodyNote(note.pitch, begin - low, end - begin, 1, source=note.source))
    result = MidiResult(sorted(notes, key=lambda n: (n.start_ms, n.pitch)), high - low, 60_000_000 / importer.tempos[max(0, bisect_right(importer.tempo_ms, low) - 1)],
                        dict(source='midi', source_name=path.name, source_start=start,
                             requested_length=(high - low) / 1000, tracks=sorted(selected), bpm_estimated=False))
    return result, duration / 1000
