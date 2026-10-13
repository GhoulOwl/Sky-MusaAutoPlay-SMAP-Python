"""Turn a pitch contour into editable notes without losing source timing."""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import mido
import numpy as np

from ..midi import SKY_MIDI, WHITE, to_sky_key
from ..score import Note, Score, ScoreError


@dataclass(frozen=True)
class MelodyNote:
    pitch: int
    start_ms: float
    duration_ms: float
    confidence: float


@dataclass
class PitchResult:
    frequency: np.ndarray
    confidence: np.ndarray
    energy_db: np.ndarray
    onset: np.ndarray
    hop_ms: float = 10
    duration_ms: float = 0
    bpm: float = 120
    metadata: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, metadata=None):
        import zipfile

        if path.stat().st_size > 16 * 1024 * 1024:
            raise ScoreError("识别结果文件过大")
        with zipfile.ZipFile(path) as archive:
            if sum(i.file_size for i in archive.infolist()) > 16 * 1024 * 1024:
                raise ScoreError("识别结果展开后过大")
        with np.load(path, allow_pickle=False) as data:
            result = cls(*(np.asarray(data[k], dtype=float) for k in
                           ("frequency", "confidence", "energy_db", "onset")),
                         hop_ms=float(data["hop_ms"]), duration_ms=float(data["duration_ms"]),
                         bpm=float(data["bpm"]), metadata=metadata or {})
        result.validate()
        return result

    def validate(self):
        arrays = (self.frequency, self.confidence, self.energy_db, self.onset)
        if (not 1 <= self.hop_ms <= 100 or not 0 < self.duration_ms <= 600_000
                or not 1 <= self.bpm <= 2000 or len(self.frequency) > 100_000
                or any(a.ndim != 1 or len(a) != len(self.frequency) or not np.isfinite(a).all()
                       for a in arrays)
                or not len(self.frequency) or (self.frequency < 0).any()
                or ((self.confidence < 0) | (self.confidence > 1)).any()):
            raise ScoreError("音高识别结果无效")

    def notes(self, threshold=.35, minimum_ms=80) -> list[MelodyNote]:
        self.validate()
        if not 0 <= threshold <= 1 or not 20 <= minimum_ms <= 1000:
            raise ScoreError("音符过滤参数无效")
        midi = 69 + 12 * np.log2(np.maximum(self.frequency, 1) / 440)
        # A short median window suppresses vibrato crossings and single-frame glitches.
        padded = np.pad(midi, (2, 2), mode="edge")
        stable = np.median(np.lib.stride_tricks.sliding_window_view(padded, 5), axis=1)
        pitches = np.rint(stable).astype(int)
        voiced = ((self.confidence >= threshold) & (self.frequency > 0)
                  & (self.energy_db >= max(-60, float(self.energy_db.max()) - 45))
                  & (pitches >= 0) & (pitches <= 127))
        notes = []
        start = None
        pitch = None
        last_onset = -1000

        def finish(end):
            if start is None:
                return
            end_ms = min(end * self.hop_ms, self.duration_ms)
            length = end_ms - start * self.hop_ms
            if length >= minimum_ms:
                notes.append(MelodyNote(int(pitch), start * self.hop_ms, length,
                                        float(np.mean(self.confidence[start:end]))))

        for i in range(len(pitches)):
            # Same-pitch re-attacks are retained, while onset chatter is debounced.
            attack = self.onset[i] >= .5 and (i - last_onset) * self.hop_ms >= minimum_ms
            if attack:
                last_onset = i
            if not voiced[i]:
                finish(i)
                start = pitch = None
            elif start is None:
                start, pitch = i, pitches[i]
            elif pitches[i] != pitch or (attack and (i - start) * self.hop_ms >= minimum_ms):
                finish(i)
                start, pitch = i, pitches[i]
        finish(len(pitches))
        return notes


def suggest_shift(notes: list[MelodyNote]) -> int:
    if not notes:
        return 0
    # Prefer duration-weighted diatonic fit, then coverage and small transposition.
    def cost(shift):
        black = sum(n.duration_ms for n in notes if (n.pitch + shift) % 12 not in WHITE)
        outside = sum(n.duration_ms for n in notes if not 60 <= n.pitch + shift <= 84)
        distance = sum(n.duration_ms * abs(n.pitch + shift - 72) for n in notes)
        return black, outside, distance, abs(shift)

    return min(range(-36, 37), key=cost)


def to_score(notes: list[MelodyNote], name: str, bpm=120, shift=None, quantize=0,
             metadata=None, author="", grid_offset_ms=0) -> tuple[Score, dict]:
    if not notes:
        raise ScoreError("未识别到可用旋律，请选择有人声的片段或降低过滤强度")
    if not math.isfinite(bpm) or not 1 <= bpm <= 2000:
        raise ScoreError("BPM 无效")
    shift = suggest_shift(notes) if shift is None else shift
    if type(shift) is not int or not -48 <= shift <= 48 or quantize not in (0, 2, 4, 8):
        raise ScoreError("移调或节拍对齐参数无效")
    if not math.isfinite(grid_offset_ms) or not -60_000 <= grid_offset_ms <= 60_000:
        raise ScoreError("网格起点无效")
    converted = []
    black = folded = 0
    previous = None
    for note in notes:
        raw = note.pitch + shift
        direction = 0 if previous is None else (raw > previous) - (raw < previous)
        key = to_sky_key(raw, direction)
        black += raw % 12 not in WHITE
        folded += not 60 <= raw <= 84
        time = note.start_ms
        duration = note.duration_ms
        if quantize:
            step = 60_000 / bpm / quantize
            time = max(0, round((time - grid_offset_ms) / step) * step + grid_offset_ms)
            duration = max(step, round(duration / step) * step)
        converted.append(Note(key, time, duration, {
            "sourcePitch": note.pitch, "sourceTime": note.start_ms,
            "sourceDuration": note.duration_ms, "pitchConfidence": round(note.confidence, 4),
            "approximated": raw % 12 not in WHITE, "folded": not 60 <= raw <= 84,
        }))
        previous = raw
    # Grid snapping can collide notes; avoid duplicate simultaneous game keys.
    unique = {}
    for note in converted:
        ident = note.key, note.time_ms
        if ident not in unique or note.duration_ms > unique[ident].duration_ms:
            unique[ident] = note
    info = dict(metadata or {}, schema=1, shift=shift, quantize=quantize, grid_offset_ms=grid_offset_ms)
    score = Score(name, bpm, author=author, transcriber="AutoSkyPlay 自动扒谱",
                  notes=sorted(unique.values(), key=lambda n: (n.time_ms, n.key)),
                  root={"transcription": info})
    return score, {"shift": shift, "semitones": black, "folded": folded, "notes": len(score.notes)}


def export_midi(notes: list[MelodyNote], path: Path, bpm=120, name="Melody"):
    """Export original pitches; tempo is metadata, never a timing rescale."""
    if not notes:
        raise ScoreError("没有可导出的音符")
    tempo = mido.bpm2tempo(bpm)
    midi = mido.MidiFile(ticks_per_beat=960, charset="utf-8")
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.extend([mido.MetaMessage("track_name", name=name), mido.MetaMessage("set_tempo", tempo=tempo)])
    events = []
    for note in notes:
        start = round(mido.second2tick(note.start_ms / 1000, 960, tempo))
        end = max(start + 1, round(mido.second2tick((note.start_ms + note.duration_ms) / 1000, 960, tempo)))
        events.extend([(start, 1, note.pitch), (end, 0, note.pitch)])
    previous = 0
    for tick, on, pitch in sorted(events):
        track.append(mido.Message("note_on" if on else "note_off", note=pitch,
                                  velocity=80 if on else 0, time=tick - previous))
        previous = tick
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    os.close(fd)
    try:
        midi.save(temporary)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sky_melody(score: Score) -> list[MelodyNote]:
    return [MelodyNote(SKY_MIDI[n.key], n.time_ms, n.duration_ms, 1) for n in score.notes]
