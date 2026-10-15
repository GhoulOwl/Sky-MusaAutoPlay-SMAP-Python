"""Evidence-gated melody routing and sparse accompaniment for a 15-key score.

The separated 'other' stem is still a mixture: only a stable dominant contour is
accepted. Ambiguous audible regions remain rests with review annotations.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from .local import MidiResult
from .melody import MelodyNote, to_score
from .performance import generation_mode, sparse_accompaniment, sparse_chords
from .register import phrase_shift
from .segmentation import runs

SOURCE_NAMES = {'vocals': '人声主导', 'other': '器乐主导', 'uncertain': '待修正', 'rest': '休止', 'midi': 'MIDI'}


def covered(notes, size, hop):
    mask = np.zeros(size, dtype=bool)
    for note in notes:
        a, b = int(note.start_ms / hop), int(np.ceil((note.start_ms + note.duration_ms) / hop))
        mask[a:b] = True
    return mask


def clip_notes(notes, start, end, source, role='melody'):
    return [replace(n, start_ms=max(start, n.start_ms),
                    duration_ms=min(end, n.start_ms + n.duration_ms) - max(start, n.start_ms),
                    source=source, role=role)
            for n in notes if min(end, n.start_ms + n.duration_ms) > max(start, n.start_ms)]


def protect_vocal_tails(mask, notes, ratio, mix_energy, hop, threshold):
    """A source-ratio drop cannot truncate an already established vocal note.

    Require a clear vocal attack and a reliable decoded note. Its acoustic
    offset remains the limit: this never joins separate notes or fills a breath.
    Explicit source overrides are applied later and still take precedence.
    """
    for note in notes:
        if note.confidence < max(.6, threshold):
            continue
        left = int(note.start_ms / hop)
        right = min(len(mask), int(np.ceil((note.start_ms + note.duration_ms) / hop)))
        anchor = max(1, int(np.ceil(min(120, note.duration_ms / 2) / hop)))
        if not (mask[left:left + anchor].all() and (ratio[left:left + anchor] >= .25).all()):
            continue
        silent = np.flatnonzero(mix_energy[left:right] < -65)
        if len(silent):
            right = left + int(silent[0])
        mask[left:right] = True


def chord_from_chroma(chroma):
    """Triads only; require all three tones and a decisive, low-contamination fit."""
    x = np.asarray(chroma, dtype=float)
    if x.shape != (12,) or not np.isfinite(x).all() or x.max() <= 0:
        return None
    # Remove the broadband chroma floor before comparing tonal templates.
    x = np.maximum(0, x - np.percentile(x, 25))
    if x.max() <= 0:
        return None
    candidates = []
    for root in range(12):
        for quality, third in (('major', 4), ('minor', 3)):
            pcs = [root, (root + third) % 12, (root + 7) % 12]
            template = np.zeros(12)
            template[pcs] = 1
            similarity = float(x @ template / max(1e-9, np.linalg.norm(x) * np.linalg.norm(template)))
            candidates.append((similarity, root, quality, pcs))
    candidates.sort(reverse=True)
    best, second = candidates[:2]
    fit, root, quality, pcs = best
    support = float(x[pcs].sum() / max(x.sum(), 1e-9))
    if fit < .8 or fit - second[0] < .08 or support < .7 or min(x[pcs]) < .22 * x.max():
        return None
    return dict(root=root, quality=quality, pitches=pcs, confidence=round(fit, 4))


def arrange(result, threshold=.35, minimum=80, mode='simple', ornaments='simplify', overrides=()):
    from .events import EventResult, arrange_events
    if mode == 'chords':
        mode = 'simple'  # Raw source candidates; voicing happens only after Sky mapping.
    if mode not in ('melody', 'simple'):
        raise ValueError('生成模式无效')
    if isinstance(result, EventResult):
        return arrange_events(result, minimum, mode, overrides)
    if isinstance(result, MidiResult):
        # Keep exact timing. Classify simultaneous notes for display/simplification only.
        groups = {}
        for note in result.notes():
            groups.setdefault(note.start_ms, []).append(note)
        notes = []
        for group in groups.values():
            ordered = sorted(group, key=lambda n: n.pitch, reverse=True)
            for i, note in enumerate(ordered[:1 if mode == 'melody' else 3]):
                notes.append(replace(note, role='melody' if i == 0 else 'accompaniment', source=note.source))
        return notes, dict(sections=[dict(start_ms=0, end_ms=result.duration_ms, source='midi', needs_review=False)],
                           chords=[], warnings=['MIDI 按同时起音的最高音选择旋律；复杂多轨请先选择主旋律音轨。'])

    vocals = result.notes(threshold, minimum, ornaments)
    a, hop, size = result.analysis, result.hop_ms, len(result.frequency)
    other = result.track('other')
    instrumental = other.notes(max(.6, threshold), max(100, minimum), ornaments) if other else []
    # Reject isolated low-confidence jumps in a polyphonic instrumental mixture.
    stable = []
    previous = None
    for note in instrumental:
        left, right = int(note.start_ms / hop), int((note.start_ms + note.duration_ms) / hop)
        prominent = np.mean(a.get('other_ratio', np.ones(size))[left:right]) >= .18
        jump = (previous is not None and note.start_ms - previous.start_ms - previous.duration_ms < 100
                and abs(note.pitch - previous.pitch) > 12 and note.confidence < .85)
        if prominent and not jump:
            stable.append(note)
            previous = note
    instrumental = stable
    vocal_mask = covered(vocals, size, hop)
    ratio = a.get('vocal_ratio', np.ones(size))
    mix_energy = a.get('mix_energy_db', result.energy_db)
    if a:
        vocal_mask &= ratio >= .16
        presence = (ratio >= .25) & (result.energy_db >= -55) & (result.confidence >= .15)
        # A weak/unclear voice protects its phrase from an instrumental takeover.
        vocal_mask |= presence
        protect_vocal_tails(vocal_mask, vocals, ratio, mix_energy, hop, threshold)
    for left, right, active in runs(vocal_mask):
        if not active and left > 0 and right < size and (right - left) * hop <= 1200:
            vocal_mask[left:right] = True
    labels = np.full(size, 'uncertain', dtype='<U12')
    labels[mix_energy < -65] = 'rest'
    labels[vocal_mask] = 'vocals'
    instrument_mask = covered(instrumental, size, hop)
    # Instrumental phrases require sustained evidence across a voice-free region.
    for left, right, active in runs(vocal_mask):
        if not active and (right - left) * hop >= 1000 and np.mean(instrument_mask[left:right]) >= .25:
            labels[left:right] = 'other'
            labels[left:right][mix_energy[left:right] < -65] = 'rest'
    # Expose missing evidence inside routed phrases instead of concealing long holes.
    for source, mask, limit in (('vocals', covered(vocals, size, hop), 300),
                                ('other', instrument_mask, 600)):
        gaps = (labels == source) & ~mask
        for left, right, missing in runs(gaps):
            if missing and (right - left) * hop >= limit:
                if source == 'vocals' and np.mean(ratio[left:right]) < .25:
                    labels[left:right] = 'rest'  # a singer's breath is not a solo cue
                else:
                    labels[left:right] = 'uncertain'
                    labels[left:right][mix_energy[left:right] < -65] = 'rest'
    for override in overrides:
        start, end, source = override['start_ms'], override['end_ms'], override['source']
        if (source not in ('vocals', 'other', 'rest') or not np.isfinite([start, end]).all()
                or not 0 <= start < end <= result.duration_ms + 1):
            raise ValueError('段落来源覆盖无效')
        labels[int(start / hop):int(np.ceil(end / hop))] = source
    notes, sections = [], []
    for left, right, source in runs(labels):
        start, end = left * hop, min(result.duration_ms, right * hop)
        selected = vocals if source == 'vocals' else instrumental if source == 'other' else []
        phrase = clip_notes(selected, start, end, source)
        # User source selection changes routing, not the evidence threshold.
        if source in ('vocals', 'other') and not phrase:
            source = 'uncertain'
        notes.extend(phrase)
        sections.append(dict(start_ms=start, end_ms=end, source=str(source), needs_review=source == 'uncertain',
                             reason='缺少可靠旋律，已保留休止' if source == 'uncertain' else ''))
    notes.sort(key=lambda n: n.start_ms)
    warnings = []
    if 'boundary_change' not in result.analysis:
        warnings.append('缺少音轨起音细节，已保守保留同音切分；重新识别可启用延音校验。')
    if other is None:
        warnings.append('此结果没有器乐分析；需使用 1.1.0 组件重新识别以补全器乐与伴奏。')
    beats = a.get('beats_ms', np.array([]))
    beat_confidence = float(a.get('beat_confidence', 0))
    chords = []
    if mode == 'simple':
        accompaniment_minimum = max(80, minimum)
        if beat_confidence < .55 or len(beats) < 4:
            warnings.append('拍点证据不足，未生成伴奏；保留旋律原始节奏。')
        else:
            bass_result = result.track('bass')
            bass_notes = bass_result.notes(max(.6, threshold), max(100, minimum)) if bass_result else []
            for i, chroma in enumerate(a.get('chroma', [])):
                start, end = float(beats[i]), float(beats[i + 1])
                section = next((s for s in sections if s['start_ms'] <= start < s['end_ms']), None)
                if not section or section['source'] in ('rest', 'uncertain'):
                    continue
                end = min(end, section['end_ms'])
                if end - start < 100:
                    continue
                chord = chord_from_chroma(chroma)
                level = a.get('harmony_energy_db', np.full(len(beats) - 1, -100))[i]
                if chord and level >= -55:
                    chords.append(dict(chord, start_ms=start, end_ms=end))
                if i % 2:  # Sparse pulse support, never claim detected downbeats.
                    continue
                # A beat may hit the final milliseconds of the previous bass
                # note. Require usable overlap; never pad that tail into a note.
                bass = next((n for n in bass_notes if n.start_ms <= start + 80
                             and min(end, n.start_ms + n.duration_ms) - max(start, n.start_ms)
                             >= accompaniment_minimum), None)
                if chord and level >= -55:
                    pcs = chord['pitches']
                    bottom = bass.pitch % 12 if bass and bass.pitch % 12 in pcs else chord['root']
                    # Candidate support tones, not a chord to play wholesale.
                    pitches = [48 + p for p in dict.fromkeys((bottom, chord['root'], (chord['root'] + 7) % 12))]
                    duration = min(450, (end - start) * .8)
                    for pitch in pitches if duration >= accompaniment_minimum else ():
                        notes.append(MelodyNote(pitch, start, duration,
                                               chord['confidence'], 'accompaniment', 'harmony', True))
                elif bass and bass.confidence >= .75:
                    bass_start = max(start, bass.start_ms)
                    duration = min(end - bass_start, 400, bass.start_ms + bass.duration_ms - bass_start)
                    if duration >= accompaniment_minimum:
                        notes.append(replace(bass, start_ms=bass_start, duration_ms=duration,
                                             role='accompaniment', source='bass'))
    return sorted(notes, key=lambda n: (n.start_ms, n.role)), dict(sections=sections, chords=chords, warnings=warnings)


def constrain_score(score, instrument='Piano'):
    """Monophonic audio lead, then one sparse, instrument-aware support voice."""
    melody = sorted((n for n in score.notes if n.extra.get('role') != 'accompaniment'), key=lambda n: n.time_ms)
    # MIDI can be legato/polyphonic; audio lead is explicitly monophonic.
    for first, second in zip(melody[:-1], melody[1:]):
        if not first.extra.get('source', '').startswith('midi') and first.time_ms < second.time_ms:
            first.duration_ms = min(first.duration_ms, second.time_ms - first.time_ms)
    return (sparse_chords if score.root['transcription'].get('mode') == 'chords' else sparse_accompaniment)(score, instrument)


def build_score(result, name, *, threshold=.35, minimum=80, mode='auto', ornaments='simplify',
                overrides=(), bpm=None, shift=None, quantize=0, grid_offset_ms=0, rhythm='gentle',
                use_detected_beats=True, author='', instrument='Piano', key_anchors=None):
    from .events import EventResult
    requested_mode = mode
    mode = generation_mode(mode, instrument)
    notes, info = arrange(result, threshold, minimum, 'simple' if mode == 'chords' else mode, ornaments, overrides)
    if shift is None:
        shift = phrase_shift(notes)
    analysis = getattr(result, 'analysis', {})
    metadata = dict(result.metadata, **info, mode=mode, ornaments=ornaments, overrides=list(overrides),
                    threshold=None if isinstance(result, EventResult) else threshold, minimum_ms=minimum,
                    analysis_version=3 if isinstance(result, EventResult) else 2,
                    arrangement_revision=7 if result.metadata.get('backend') == 'hybrid'
                    else 6 if isinstance(result, EventResult) else 5,
                    generation_mode=requested_mode, instrument=instrument,
                    beats_ms=np.asarray(analysis.get('beats_ms', [])).tolist(),
                    beat_confidence=float(analysis.get('beat_confidence', 0)),
                    use_detected_beats=use_detected_beats)
    score, stats = to_score(notes, name, bpm or result.bpm, shift, quantize, metadata, author, grid_offset_ms,
                            beats=analysis.get('beats_ms', ()) if use_detected_beats else (), rhythm=rhythm,
                            beat_confidence=float(analysis.get('beat_confidence', 0)) if use_detected_beats else 0,
                            allow_empty=True, key_anchors=key_anchors)
    # Snapping must not move notes into deliberately empty/uncertain regions.
    if not isinstance(result, MidiResult):
        retained = []
        for note in score.notes:
            origin = note.extra['sourceTime']
            section = next((s for s in info['sections'] if s['start_ms'] <= origin < s['end_ms']), None)
            if section and section['source'] not in ('rest', 'uncertain'):
                end = min(section['end_ms'], note.time_ms + note.duration_ms)
                note.time_ms = max(section['start_ms'], note.time_ms)
                note.duration_ms = end - note.time_ms
                floor = max(80, minimum) if note.extra.get('role') == 'accompaniment' else 0
                if note.duration_ms > 0 and note.duration_ms + 1e-6 >= floor:
                    retained.append(note)
        score.notes = retained
    # Apply key/polyphony constraints to the final intervals, after eliminating
    # tiny fragments so they cannot displace a valid accompanying voice.
    constrain_score(score, instrument)
    score.root['transcription']['mapping_review'] = [
        dict(start_ms=n.time_ms, end_ms=n.time_ms + n.duration_ms, reason=n.extra['mappingReview'])
        for n in score.notes if n.extra.get('mappingReview')]
    if mode in ('simple', 'chords') and not any(n.extra.get('role') == 'accompaniment' for n in score.notes):
        score.root['transcription']['warnings'].append('未获得符合稀疏编配要求的伴奏（证据、音域或叠响限制），已保留主旋律。')
    stats.update(semitones=sum(n.extra.get('approximated', False) for n in score.notes),
                 folded=sum(n.extra.get('folded', False) for n in score.notes),
                 notes=len(score.notes), accompaniment=sum(n.extra.get('role') == 'accompaniment' for n in score.notes),
                 review=sum(s['needs_review'] for s in info['sections']),
                 mapping_review=len(score.root['transcription'].get('mapping_review', [])))
    return score, stats
