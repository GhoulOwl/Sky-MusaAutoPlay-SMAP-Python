"""MIDI conversion including tempo events from non-selected conductor tracks."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict, deque
from pathlib import Path
from statistics import median

import mido

from .score import Note, Score, ScoreError

SKY_MIDI = (60, 62, 64, 65, 67, 69, 71, 72, 74, 76, 77, 79, 81, 83, 84)
WHITE = {0, 2, 4, 5, 7, 9, 11}


def track_name(value):
    # Mido defaults to Latin-1 for arbitrary MIDI text. Recover UTF-8 titles
    # produced by modern exporters while retaining older single-byte names.
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value


def to_sky_key(pitch: int, direction: int = 0) -> int:
    while pitch < 60:
        pitch += 12
    while pitch > 84:
        pitch -= 12
    if pitch % 12 not in WHITE:
        pitch += 1 if direction > 0 else -1
    return SKY_MIDI.index(pitch)


class MidiImporter:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.file = mido.MidiFile(path)
        if self.file.type == 2 or self.file.ticks_per_beat <= 0:
            raise ScoreError("仅支持同步 MIDI type 0/1 和 PPQ 时间制")
        self.events = []
        tempos = {0: 500_000}
        for track in self.file.tracks:
            tick = 0
            events = []
            for msg in track:
                tick += msg.time
                events.append((tick, msg))
                if msg.type == "set_tempo":
                    if msg.tempo <= 0:
                        raise ScoreError("MIDI 包含无效速度")
                    tempos[tick] = msg.tempo
            self.events.append(events)
        self.tempo_ticks = sorted(tempos)
        self.tempos = [tempos[t] for t in self.tempo_ticks]
        self.tempo_ms = [0.0]
        for i in range(1, len(self.tempo_ticks)):
            delta = self.tempo_ticks[i] - self.tempo_ticks[i - 1]
            self.tempo_ms.append(
                self.tempo_ms[-1] + delta * self.tempos[i - 1] / self.file.ticks_per_beat / 1000
            )

    @property
    def bpm(self) -> float:
        return 60_000_000 / self.tempos[0]

    def tracks(self) -> list[tuple[int, str, int]]:
        return [
            (
                i,
                track_name(self.file.tracks[i].name) or f"Track {i + 1}",
                sum(m.type == "note_on" and m.velocity > 0 for _, m in events),
            )
            for i, events in enumerate(self.events)
        ]

    def pitches(self, tracks: set[int]) -> list[int]:
        return [
            m.note
            for i in sorted(tracks)
            for _, m in self.events[i]
            if m.type == "note_on" and m.velocity > 0 and m.channel != 9
        ]

    def suggest_shift(self, tracks: set[int]) -> int:
        pitches = self.pitches(tracks)
        if not pitches:
            return 0
        shifts = [s if s <= 6 else s - 12 for s in range(12)]
        shift = max(shifts, key=lambda s: (sum((p + s) % 12 in WHITE for p in pitches), -abs(s)))
        return shift + 12 * round((72 - median(pitches) - shift) / 12)

    def tick_ms(self, tick: int) -> float:
        i = bisect_right(self.tempo_ticks, tick) - 1
        return self.tempo_ms[i] + (
            (tick - self.tempo_ticks[i]) * self.tempos[i] / self.file.ticks_per_beat / 1000
        )

    def convert(
        self, tracks: set[int] | None = None, shift: int | None = None, include_drums: bool = False
    ) -> Score:
        tracks = tracks if tracks is not None else set(range(len(self.events)))
        if not tracks or any(i < 0 or i >= len(self.events) for i in tracks):
            raise ScoreError("请选择有效音轨")
        shift = self.suggest_shift(tracks) if shift is None else shift
        notes = []
        for i in sorted(tracks):
            active = defaultdict(deque)
            previous = None
            for tick, msg in self.events[i]:
                if msg.type not in ("note_on", "note_off"):
                    continue
                if msg.channel == 9 and not include_drums:
                    continue
                ident = (msg.channel, msg.note)
                if msg.type == "note_on" and msg.velocity > 0:
                    raw = msg.note + shift
                    direction = 0 if previous is None else (raw > previous) - (raw < previous)
                    note = Note(to_sky_key(raw, direction), self.tick_ms(tick))
                    active[ident].append(note)
                    notes.append(note)
                    previous = raw
                elif active[ident]:
                    note = active[ident].popleft()
                    note.duration_ms = max(20, self.tick_ms(tick) - note.time_ms)
        unique: dict[tuple[int, float], Note] = {}
        for note in notes:
            ident = (note.key, round(note.time_ms, 4))
            if ident not in unique or note.duration_ms > unique[ident].duration_ms:
                unique[ident] = note
        if not unique:
            raise ScoreError("所选音轨没有可转换的旋律音符")
        return Score(self.path.stem, self.bpm, notes=sorted(unique.values(), key=lambda n: n.time_ms))
