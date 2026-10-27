"""Native polyphonic model results; never reinterpret them as trusted MIDI."""
from __future__ import annotations

import json
import math
import os
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import mido
import numpy as np

from ..score import atomic_json
from .melody import MelodyNote


@dataclass(frozen=True)
class ModelNote:
    pitch: int
    start_ms: float
    duration_ms: float
    instrument: str
    index: int
    # Input provenance is independent of the model's instrument classification.
    provider: str = 'muscriptor'
    audio_source: str = 'original'
    event_id: str = ''
    raw_pitch_midi: float | None = None
    model_confidence: float | None = None
    review_reasons: tuple[str, ...] = ()

    @property
    def identity(self):
        return self.event_id or f'{self.provider}:{self.audio_source}:{self.index}'

    @property
    def source(self):
        if self.audio_source != 'original':
            return 'vocals' if self.audio_source == 'vocals' else 'other'
        return 'vocals' if self.instrument == 'voice' else 'other'

    def melody(self, role='melody'):
        return MelodyNote(self.pitch, self.start_ms, self.duration_ms, self.model_confidence,
                          role=role, source=self.source, event_id=self.event_id or self.index,
                          instrument=self.instrument, provider=self.provider, audio_source=self.audio_source,
                          raw_pitch_midi=self.raw_pitch_midi, review_reasons=self.review_reasons)


@dataclass
class EventResult:
    raw_notes: list[ModelNote]
    duration_ms: float
    bpm: float = 120
    metadata: dict = field(default_factory=dict)
    analysis: dict = field(default_factory=dict)

    def validate(self):
        if not isinstance(self.metadata, dict) or not isinstance(self.analysis, dict):
            raise ValueError('音符事件元数据无效')
        if (not math.isfinite(self.duration_ms) or not 0 < self.duration_ms <= 600000
                or not math.isfinite(self.bpm) or not 30 <= self.bpm <= 300 or len(self.raw_notes) > 200000):
            raise ValueError('音符事件结果无效')
        ids, identities = set(), set()
        for n in self.raw_notes:
            if (type(n.pitch) is not int or not 0 <= n.pitch <= 127 or type(n.index) is not int
                    or n.index in ids or not isinstance(n.instrument, str) or len(n.instrument) > 80
                    or not all(math.isfinite(t) for t in (n.start_ms, n.duration_ms))
                    or n.start_ms < 0 or n.duration_ms <= 0
                    or n.start_ms + n.duration_ms > self.duration_ms + 1):
                raise ValueError('音符事件包含无效时间、音高或重复 ID')
            if (n.identity in identities or any(not isinstance(s, str) or len(s) > 160
                    for s in (n.provider, n.audio_source, n.event_id))
                    or n.raw_pitch_midi is not None and (not math.isfinite(n.raw_pitch_midi)
                                                        or not 0 <= n.raw_pitch_midi <= 128)
                    or n.model_confidence is not None and (not math.isfinite(n.model_confidence)
                                                          or not 0 <= n.model_confidence <= 1)
                    or not isinstance(n.review_reasons, (tuple, list))
                    or any(not isinstance(s, str) or len(s) > 240 for s in n.review_reasons)):
                raise ValueError('音符来源、分数或原始音高无效')
            ids.add(n.index)
            identities.add(n.identity)
        beats = np.asarray(self.analysis.get('beats_ms', []))
        if (beats.ndim != 1 or not np.isfinite(beats).all() or len(beats) > 10000
                or (beats < 0).any() or (beats > self.duration_ms).any() or (np.diff(beats) <= 0).any()):
            raise ValueError('拍点无效')
        confidence = self.analysis.get('beat_confidence', 0)
        energy = np.asarray(self.analysis.get('energy_db', []))
        if (not isinstance(confidence, (float, int)) or not 0 <= confidence <= 1
                or energy.ndim != 1 or len(energy) > 6001 or not np.isfinite(energy).all()):
            raise ValueError('音频分析无效')
        if 'chroma' in self.analysis:
            chroma = np.asarray(self.analysis['chroma'], dtype=float)
            levels = np.asarray(self.analysis.get('harmony_energy_db', []), dtype=float)
            count = max(0, len(beats)-1)
            if (chroma.size and chroma.shape != (count, 12) or levels.shape != (count,)
                    or not np.isfinite(chroma).all() or not np.isfinite(levels).all()
                    or (chroma < 0).any()):
                raise ValueError('和声证据格式无效')

    @classmethod
    def load(cls, path, metadata=None):
        if Path(path).stat().st_size > 40 * 1024**2:
            raise ValueError('音符事件文件过大')
        try:
            data = json.loads(Path(path).read_text('utf-8'))
            if data.get('schema') not in (1, 2):
                raise ValueError('不支持的音符事件版本')
            result = cls([ModelNote(**dict(n, review_reasons=tuple(n.get('review_reasons', ()))))
                          for n in data['notes']], data['duration_ms'], data['bpm'],
                         metadata if metadata is not None else data.get('metadata', {}), data.get('analysis', {}))
            result.validate()
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('音符事件文件结构无效') from exc
        return result

    def save(self, path):
        self.validate()
        atomic_json(Path(path), dict(schema=2, notes=[asdict(n) for n in self.raw_notes],
                                    duration_ms=self.duration_ms, bpm=self.bpm,
                                    metadata=self.metadata, analysis=self.analysis))

    def notes(self, *_):
        return [n.melody() for n in self.raw_notes if n.instrument != 'drums']

    def export_midi(self, path):
        """Keep every decoded voice and exact seconds; velocity is a placeholder."""
        if not self.raw_notes:
            raise ValueError('没有可导出的模型音符')
        programs = dict(zip(
            ('acoustic_piano electric_piano chromatic_percussion organ acoustic_guitar clean_electric_guitar '
             'distorted_electric_guitar acoustic_bass electric_bass violin viola cello contrabass orchestral_harp '
             'timpani string_ensemble synth_strings voice orchestra_hit trumpet trombone tuba french_horn '
             'brass_section soprano_and_alto_sax tenor_sax baritone_sax oboe english_horn bassoon clarinet '
             'flutes synth_lead synth_pad').split(),
            (0, 2, 8, 16, 24, 26, 29, 32, 33, 40, 41, 42, 43, 46, 47, 48, 50, 52, 55, 56, 57,
             58, 60, 61, 64, 66, 67, 68, 69, 70, 71, 72, 80, 88)))
        midi = mido.MidiFile(ticks_per_beat=1000, charset='utf-8')
        groups = defaultdict(list)
        for note in self.raw_notes:
            groups[(note.provider, note.audio_source, note.instrument)].append(note)
        channels = [n for n in range(16) if n != 9]
        for i, ((provider, source, name), notes) in enumerate(groups.items()):
            track = mido.MidiTrack()
            midi.tracks.append(track)
            channel = 9 if name == 'drums' else channels[i % 15]
            track.extend([mido.MetaMessage('track_name', name=f'{provider}/{source}/{name}'),
                          mido.MetaMessage('set_tempo', tempo=1000000),
                          mido.MetaMessage('midi_port', port=i//15),
                          mido.MetaMessage('text', text='Fixed velocity; fractional MIDI pitches rounded; no pitch bend')])
            program = programs.get(name, int(name[8:]) if name.startswith('program_') and name[8:].isdigit() else 0)
            if name != 'drums':
                track.append(mido.Message('program_change', program=min(127, program), channel=channel))
            events = []
            for n in notes:
                a = round(n.start_ms)
                b = max(a+1, round(n.start_ms+n.duration_ms))
                events.extend([(a, 1, n.pitch), (b, 0, n.pitch)])
            last = 0
            for tick, on, pitch in sorted(events):
                track.append(mido.Message('note_on' if on else 'note_off', channel=channel,
                                          note=pitch, velocity=80 if on else 0, time=tick-last))
                last = tick
        path = Path(path)
        fd, temporary = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
        os.close(fd)
        try:
            midi.save(temporary)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def _line(notes):
    """Choose one continuous path through onset groups, without slicing held notes."""
    groups = []
    for note in sorted(notes, key=lambda n: (n.start_ms, n.index)):
        if groups and note.start_ms - groups[-1][0].start_ms < 35:
            groups[-1].append(note)
        else:
            groups.append([note])
    if not groups:
        return [], 0
    costs, parents = [], []
    for i, group in enumerate(groups):
        local = np.array([abs(n.pitch - 67) * .035 - min(n.duration_ms, 1000) / 2500 for n in group])
        if not i:
            costs.append(local)
            parents.append(np.zeros(len(group), int))
        else:
            distance = np.array([[abs(a.pitch - b.pitch) for b in group] for a in groups[i-1]])
            matrix = costs[-1][:, None] + distance * .12 + (distance > 12) * 2
            parents.append(np.argmin(matrix, axis=0))
            costs.append(local + np.min(matrix, axis=0))
    chosen, key = [], int(np.argmin(costs[-1]))
    for i in range(len(groups)-1, -1, -1):
        chosen.append(groups[i][key])
        key = int(parents[i][key])
    chosen.reverse()
    monophony = len(groups) / sum(len(g) for g in groups)
    return chosen, monophony


def arrange_events(result, minimum=80, mode='simple', overrides=()):
    """Phrase continuity is a routing heuristic, never a calibrated probability.

    Source decisions use context, but emitted intervals keep their model offsets.
    Unknown regions stay empty. No same-key merging or frame-contour decoding.
    """
    if result.metadata.get('backend') == 'hybrid':
        from .hybrid_arrangement import arrange_hybrid
        return arrange_hybrid(result, minimum, mode, overrides)
    grouped = defaultdict(list)
    energy = np.asarray(result.analysis.get('energy_db', []))
    for note in result.raw_notes:
        support = energy[int(note.start_ms/100):max(int(note.start_ms/100)+1,
                         math.ceil((note.start_ms+note.duration_ms)/100))]
        if len(support) and np.max(support) < -60:
            continue
        if (note.instrument not in ('drums', 'electric_bass', 'acoustic_bass')
                and 45 <= note.pitch <= 96 and note.duration_ms >= minimum):
            grouped[note.instrument].append(note)
    lines = {name: _line(notes) for name, notes in grouped.items()}
    width = 2500
    routing, previous = {}, None
    for start in range(0, math.ceil(result.duration_ms), width):
        left, right = max(0, start - 1500), min(result.duration_ms, start + width + 1500)
        candidates = []
        for name, (notes, monophony) in lines.items():
            active = [n for n in notes if n.start_ms < right and n.start_ms + n.duration_ms > left]
            if not active:
                continue
            coverage = min(1., sum(min(right, n.start_ms+n.duration_ms)-max(left, n.start_ms)
                                   for n in active) / max(1, right-left))
            if len(active) < 2 and active[0].duration_ms < 450:
                continue
            jumps = np.mean([min(24, abs(a.pitch-b.pitch)) for a, b in zip(active, active[1:])]) if len(active)>1 else 0
            score = coverage + .7 * monophony - jumps * .025
            score += .55 if name == 'voice' else 0
            score += .20 if name == previous else 0
            candidates.append((score, name))
        candidates.sort(reverse=True)
        winner = candidates[0][1] if candidates and candidates[0][0] >= .7 else None
        if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < .18:
            winner = None
        routing[start // width] = winner
        if winner:
            previous = winner
    selected = sorted((n for name, (line, _) in lines.items() for n in line
                       if routing.get(int(n.start_ms // width)) == name), key=lambda n: (n.start_ms, n.index))
    lead = []
    for note in selected:
        if lead and note.start_ms < lead[-1].start_ms + lead[-1].duration_ms:
            if note.instrument != lead[-1].instrument:
                continue  # A different source cannot truncate an established sustained note.
            # Within one source, new pitches/reattacks define the next monophonic onset.
            duration = note.start_ms - lead[-1].start_ms
            if duration <= 0:
                continue
            lead[-1] = replace(lead[-1], duration_ms=duration)
        lead.append(note)
    notes = [MelodyNote(n.pitch, n.start_ms, n.duration_ms, None, source='vocals' if n.instrument == 'voice'
                       else 'other', event_id=n.index, instrument=n.instrument) for n in lead]
    for override in overrides:
        a, b, source = override['start_ms'], override['end_ms'], override['source']
        if (source not in ('vocals', 'other', 'rest') or not np.isfinite([a, b]).all()
                or not 0 <= a < b <= result.duration_ms + 1):
            raise ValueError('段落来源覆盖无效')
        kept = []
        for n in notes:
            end = n.start_ms + n.duration_ms
            if end <= a or n.start_ms >= b or n.source == source:
                kept.append(n)
            else:
                if n.start_ms < a:
                    kept.append(replace(n, duration_ms=a-n.start_ms))
                if end > b:
                    kept.append(replace(n, start_ms=b, duration_ms=end-b))
        notes = kept
    # Exact boundaries avoid chopping at analysis-window edges or quantized frame positions.
    boundaries = sorted({0., result.duration_ms, *(n.start_ms for n in notes),
                         *(n.start_ms+n.duration_ms for n in notes),
                         *(t for o in overrides for t in (o['start_ms'], o['end_ms']))})
    sections = []
    for a, b in zip(boundaries, boundaries[1:]):
        active = next((n for n in notes if n.start_ms <= a and n.start_ms+n.duration_ms >= b-1e-6), None)
        source = active.source if active else 'rest'
        quiet = len(energy) and np.max(energy[int(a/100):max(int(a/100)+1, math.ceil(b/100))], initial=-100) < -60
        if not active and b-a >= 800 and not quiet:
            source = 'uncertain'
        if any(o['source'] == 'rest' and o['start_ms'] <= a and o['end_ms'] >= b for o in overrides):
            source = 'rest'
        section = dict(start_ms=a, end_ms=b, source=source, needs_review=source == 'uncertain',
                       reason='旋律来源不明确或缺少完整音符，已保留休止' if source == 'uncertain' else '')
        if sections and sections[-1]['source'] == source:
            sections[-1]['end_ms'] = b
        else:
            sections.append(section)
    if mode == 'simple':
        selected_ids = {n.event_id for n in notes}
        for n in result.raw_notes:
            if n.index in selected_ids or n.instrument in ('voice', 'drums') or n.duration_ms < max(80, minimum):
                continue
            active = next((m for m in notes if m.role == 'melody' and m.start_ms <= n.start_ms
                           and m.start_ms+m.duration_ms > n.start_ms and m.pitch-n.pitch >= 5), None)
            if active:
                end = min(n.start_ms+n.duration_ms, active.start_ms+active.duration_ms)
                if end-n.start_ms >= max(80, minimum):
                    notes.append(MelodyNote(n.pitch, n.start_ms, end-n.start_ms, None, 'accompaniment',
                                           active.source, False, n.index, n.instrument))
    return sorted(notes, key=lambda n: (n.start_ms, n.role)), dict(sections=sections, chords=[], warnings=[
        'MuScriptor 乐器标签可能混淆人声；旋律来源按连续性筛选，待修正位置需试听复核。',
        *result.metadata.get('warnings', [])])
