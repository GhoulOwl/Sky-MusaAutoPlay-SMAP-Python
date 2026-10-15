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
    confidence: float | None
    role: str = "melody"
    source: str = "vocals"
    generated: bool = False
    event_id: int | str | None = None
    instrument: str = ''
    provider: str = ''
    audio_source: str = ''
    raw_pitch_midi: float | None = None
    review_reasons: tuple[str, ...] = ()


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
    analysis: dict = field(default_factory=dict)

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
                         bpm=float(data["bpm"]), metadata=metadata or {},
                         analysis={k[2:]: np.asarray(data[k], dtype=float) for k in data.files if k.startswith("a_")})
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

        for key, value in self.analysis.items():
            a = np.asarray(value)
            if a.size > 800000 or not np.isfinite(a).all():
                raise ScoreError('分层分析数据无效')
            if key in ('beats_ms',):
                if a.ndim != 1 or (a < 0).any() or (a > self.duration_ms).any() or (np.diff(a) <= 0).any():
                    raise ScoreError('拍点数据无效')
            elif key == 'chroma':
                if a.ndim != 2 or a.shape[1] != 12 or (a < 0).any():
                    raise ScoreError('和声数据无效')
            elif key == 'beat_confidence':
                if a.size != 1 or not 0 <= float(a.reshape(-1)[0]) <= 1:
                    raise ScoreError('节拍可靠度无效')
            elif key == 'harmony_energy_db':
                if a.ndim != 1:
                    raise ScoreError('和声能量无效')
            elif a.ndim != 1 or len(a) != len(self.frequency):
                raise ScoreError('音轨帧数不一致')
            if key.endswith('boundary_change') and ((a < -1).any() or (a > 2).any()):
                raise ScoreError('起音连续性数据无效')
        for name in ('other', 'bass'):
            present = [name + '_' + k in self.analysis for k in ('frequency', 'confidence', 'energy_db', 'onset')]
            if any(present):
                if not all(present):
                    raise ScoreError('音轨分析不完整')
                self.track(name).validate()
        if 'chroma' in self.analysis and len(self.analysis['chroma']) != max(0, len(self.analysis.get('beats_ms', [])) - 1):
            raise ScoreError('和声与拍点不一致')
        if 'harmony_energy_db' in self.analysis and len(self.analysis['harmony_energy_db']) != len(self.analysis.get('chroma', [])):
            raise ScoreError('和声能量与拍点不一致')

    def arrays(self):
        data = {k: getattr(self, k) for k in
                ('frequency', 'confidence', 'energy_db', 'onset', 'hop_ms', 'duration_ms', 'bpm')}
        data.update({'a_' + k: v for k, v in self.analysis.items()})
        return data

    def track(self, name):
        names = ('frequency', 'confidence', 'energy_db', 'onset')
        if not all(name + '_' + k in self.analysis for k in names):
            return None
        return PitchResult(*(self.analysis[name + '_' + k] for k in names),
                           self.hop_ms, self.duration_ms, self.bpm,
                           analysis={'boundary_change': self.analysis[name + '_boundary_change']}
                           if name + '_boundary_change' in self.analysis else {})

    def notes(self, threshold=.35, minimum_ms=80, ornaments='simplify') -> list[MelodyNote]:
        from .segmentation import decode
        return decode(self, threshold, minimum_ms, ornaments)


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
             metadata=None, author="", grid_offset_ms=0, *, beats=(), rhythm="strict",
             beat_confidence=0, allow_empty=False) -> tuple[Score, dict]:
    if not notes and not allow_empty:
        raise ScoreError("未识别到可用旋律，请选择其他片段或检查待修正标记")
    if not math.isfinite(bpm) or not 1 <= bpm <= 2000:
        raise ScoreError("BPM 无效")
    shift = suggest_shift([n for n in notes if n.role == "melody"] or notes) if shift is None else shift
    if type(shift) is not int or not -48 <= shift <= 48 or quantize not in (0, 2, 3, 4, 6, 8):
        raise ScoreError("移调或节拍对齐参数无效")
    if not math.isfinite(grid_offset_ms) or not -60_000 <= grid_offset_ms <= 60_000:
        raise ScoreError("网格起点无效")
    if rhythm not in ('original', 'gentle', 'strict'):
        raise ScoreError('节奏整理模式无效')
    from .register import melody_keys
    from .rhythm import snap_interval
    mapped = melody_keys(notes, shift)
    converted = []
    black = folded = 0
    previous = None
    for index, note in enumerate(notes):
        raw = note.pitch + shift
        direction = 0 if previous is None else (raw > previous) - (raw < previous)
        key = mapped.get(index, to_sky_key(raw, direction))
        black += raw % 12 not in WHITE
        folded += not 60 <= raw <= 84
        time = note.start_ms
        duration = note.duration_ms
        if quantize or rhythm == 'gentle':
            time, end = snap_interval(time, time + duration, bpm, quantize, grid_offset_ms,
                                      beats, rhythm, beat_confidence)
            duration = end - time
        converted.append(Note(key, time, duration, {
            "sourcePitch": note.pitch, "sourceTime": note.start_ms,
            "sourceDuration": note.duration_ms, "pitchConfidence": round(note.confidence, 4) if note.confidence is not None else None,
            **({'modelEvent': note.event_id, 'modelInstrument': note.instrument} if note.event_id is not None else {}),
            **({'provider': note.provider, 'audioSource': note.audio_source,
                'rawPitchMidi': note.raw_pitch_midi, 'reviewReasons': list(note.review_reasons)}
               if note.provider else {}),
            "approximated": raw % 12 not in WHITE, "folded": abs(SKY_MIDI[key] - raw) >= 11,
            "role": note.role, "source": note.source, "generated": note.generated,
        }))
        previous = raw
    collisions = {}
    for note in converted:
        if note.extra['role'] == 'melody' and not note.extra['source'].startswith('midi'):
            collisions.setdefault(note.time_ms, []).append(note)
    for group in collisions.values():
        if len({n.extra['sourceTime'] for n in group}) > 1:
            for note in group:
                note.time_ms = note.extra['sourceTime']
                note.duration_ms = note.extra['sourceDuration']
    # Grid snapping can collide notes; avoid duplicate simultaneous game keys.
    unique = {}
    for note in converted:
        ident = note.key, note.time_ms, note.extra["role"]
        if (ident not in unique or
                (note.extra['role'] == 'melody', note.duration_ms) >
                (unique[ident].extra['role'] == 'melody', unique[ident].duration_ms)):
            unique[ident] = note
    info = dict(metadata or {}, schema=2, shift=shift, quantize=quantize, grid_offset_ms=grid_offset_ms, rhythm=rhythm)
    mapping_review = []
    lead = sorted((n for n in unique.values() if n.extra['role'] == 'melody'), key=lambda n: n.time_ms)
    for i, note in enumerate(lead):
        reason = '长音含光遇无法直接演奏的半音' if note.extra['approximated'] and note.duration_ms >= 500 else ''
        if i:
            previous_note = lead[i - 1]
            raw_interval = note.extra['sourcePitch'] - previous_note.extra['sourcePitch']
            mapped_interval = SKY_MIDI[note.key] - SKY_MIDI[previous_note.key]
            if (note.extra['source'] == previous_note.extra['source']
                    and note.time_ms - previous_note.time_ms - previous_note.duration_ms <= 600):
                if raw_interval * mapped_interval < 0:
                    reason = '光遇音域限制改变了旋律方向'
                elif raw_interval and mapped_interval == 0:
                    reason = '相邻音高在光遇键位上重合'
                elif abs(raw_interval) <= 5 and abs(mapped_interval) >= 10:
                    reason = '光遇音域映射扩大了旋律跳进'
        if reason:
            note.extra['mappingReview'] = reason
            mapping_review.append(dict(start_ms=note.time_ms, end_ms=note.time_ms + note.duration_ms, reason=reason))
    info['mapping_review'] = mapping_review
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
    groups = {}
    for note in notes:
        groups.setdefault((note.source, note.role), []).append(note)
    for index, ((source, role), group) in enumerate(groups.items()):
        track = mido.MidiTrack()
        midi.tracks.append(track)
        title = name if len(groups) == 1 else f'{name} · {source} · {role}'
        track.extend([mido.MetaMessage('track_name', name=title), mido.MetaMessage('set_tempo', tempo=tempo)])
        events = []
        for note in group:
            start = round(mido.second2tick(note.start_ms / 1000, 960, tempo))
            end = max(start + 1, round(mido.second2tick((note.start_ms + note.duration_ms) / 1000, 960, tempo)))
            events.extend([(start, 1, note.pitch), (end, 0, note.pitch)])
        previous = 0
        for tick, on, pitch in sorted(events):
            track.append(mido.Message('note_on' if on else 'note_off', note=pitch,
                                      channel=index % 9, velocity=80 if on else 0, time=tick - previous))
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
