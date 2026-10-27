"""Evidence-backed arrangement with independent melody and harmony coverage."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import replace
from functools import lru_cache

import numpy as np
import soundfile as sf

from ..audio import ASSETS, SAMPLE_RATE, SUSTAIN, loop_sample, resample
from ..midi import SKY_MIDI
from ..score import Note, ScoreError
from .events import EventResult
from .harmonic_structure import chord_path, phrase_boundaries, pulse_from_evidence, voice_path
from .local import MidiResult
from .melody import MelodyNote

POLICY = 'rich-harmony-4'
MAXIMUM_KEYS = 3


@lru_cache(maxsize=64)
def strong_tails(instrument):
    """Only the strong envelope constrains new harmony, not the entire decay."""
    if instrument in SUSTAIN:
        return (0.,) * 15
    tails = []
    for key in range(15):
        y, sr = sf.read(ASSETS / 'instruments' / instrument / f'{key}.wav', always_2d=True)
        hop = max(1, round(sr * .02))
        rms = np.array([np.sqrt(np.mean(y[i:i+hop] ** 2)) for i in range(0, len(y), hop)])
        audible = np.flatnonzero(rms > rms.max(initial=0) * 10 ** (-18 / 20))
        tails.append(float((audible[-1] + 1) * hop / sr * 1000) if len(audible) else 0.)
    return tuple(tails)


@lru_cache(maxsize=64)
def sample_energy(instrument):
    """Cumulative sample power in milliseconds; same bank/loops as the mixer."""
    envelopes = []
    for key in range(15):
        data, sr = sf.read(ASSETS / 'instruments' / instrument / f'{key}.wav',
                           dtype='float32', always_2d=True)
        data = np.repeat(data, 2, axis=1) if data.shape[1] == 1 else data[:, :2]
        data = resample(data, sr / SAMPLE_RATE)
        if instrument in SUSTAIN:
            power = float(np.mean(loop_sample(data) ** 2))
            envelopes.append((np.array([0.]), np.array([0.]), power))
        else:
            hop = SAMPLE_RATE // 50
            edges = np.arange(0, len(data), hop)
            power = np.sum(data.astype(float) ** 2, axis=1) / data.shape[1]
            areas = np.add.reduceat(power, edges) * 1000 / SAMPLE_RATE
            times = np.r_[edges, len(data)] * 1000 / SAMPLE_RATE
            envelopes.append((times, np.r_[0., np.cumsum(areas)], 0.))
    return tuple(envelopes)


def interval_energy(envelope, start, duration, left, right):
    times, area, sustain_power = envelope
    a, b = max(0., left-start), max(0., right-start)
    if sustain_power:
        release = 1500/44.1
        def integral(t):
            tail = min(release, max(0., t-duration))
            return sustain_power * (min(t, duration) + tail-tail**2/release+tail**3/(3*release**2))
        return integral(b)-integral(a)
    return float(np.interp(b, times, area)-np.interp(a, times, area))


def exclusions(result, overrides):
    """A missing lead is not silence. Only acoustic silence/user rests exclude harmony."""
    from .segmentation import runs
    analysis = getattr(result, 'analysis', {})
    energy = np.asarray(analysis.get('energy_db', analysis.get('mix_energy_db', getattr(result, 'energy_db', []))))
    hop = analysis.get('evidence_hop_ms', getattr(result, 'hop_ms', 100))
    blocked = [(a * hop, min(result.duration_ms, b * hop)) for a, b, quiet in runs(energy < -60)
               if quiet] if len(energy) else []
    blocked.extend((o['start_ms'], o['end_ms']) for o in overrides if o['source'] == 'rest')
    return sorted(blocked)


def allowed_parts(start, end, blocked):
    parts = [(start, end)]
    for a, b in blocked:
        if a >= end:
            break
        parts = [(x, y) for left, right in parts
                 for x, y in ([(left, right)] if b <= left or a >= right else
                              [(left, min(right, a)), (max(left, b), right)]) if y > x]
    return parts


def evidence_notes(result, minimum):
    if isinstance(result, EventResult):
        return [n.melody('accompaniment') for n in result.raw_notes
                if n.instrument not in ('voice', 'drums') and n.audio_source != 'vocals'
                and n.duration_ms >= minimum]
    if isinstance(result, MidiResult):
        return [replace(n, role='accompaniment', event_id=f'midi:{i}')
                for i, n in enumerate(result.raw_notes) if n.duration_ms >= minimum]
    notes = []
    for name in ('other', 'bass'):
        track = result.track(name)
        if track:
            notes.extend(replace(n, role='accompaniment', source=name, event_id=f'{name}:{i}',
                                 instrument='acoustic_bass' if name == 'bass' else 'other')
                         for i, n in enumerate(track.notes(.6, max(100, minimum))))
    return notes


def harmony_windows(result, evidence, blocked):
    """Fit major/minor triads; a missing fifth needs an observed root bass and third."""
    from .arrangement import chord_from_chroma
    analysis = getattr(result, 'analysis', {})
    beats = np.asarray(analysis.get('beats_ms', []), dtype=float)
    reliable = analysis.get('beat_confidence', 0) >= .55 and len(beats) >= 4
    boundaries = (sorted({0., result.duration_ms, *beats}) if reliable else
                  sorted({n.start_ms for n in evidence} | {n.start_ms+n.duration_ms for n in evidence}))
    observations, reviews = [], []
    chroma = analysis.get('chroma', [])
    levels = analysis.get('harmony_energy_db', [])
    ordered = sorted(evidence, key=lambda n: n.start_ms)
    cursor, active = 0, []
    for start, end in zip(boundaries, boundaries[1:]):
        while cursor < len(ordered) and ordered[cursor].start_ms < end:
            active.append(ordered[cursor])
            cursor += 1
        active = [n for n in active if n.start_ms+n.duration_ms > start]
        if end-start < 80:
            continue
        hist = np.zeros(12)
        for n in active:
            hist[n.pitch % 12] += min(end, n.start_ms+n.duration_ms) - max(start, n.start_ms)
        index = int(np.searchsorted(beats, start+1e-6)-1)
        acoustic = (reliable and 0 <= index < len(chroma) and index < len(levels) and levels[index] >= -55)
        features = np.asarray(chroma[index], dtype=float) if acoustic else np.zeros(12)
        # Agreement reinforces existing pitches; independent clean chroma can also stand alone.
        combined = hist / max(hist.max(), 1) + features / max(float(features.max(initial=0)), 1e-9)
        chord = chord_from_chroma(combined)
        if chord is None and hist.max() > 0:
            pcs = set(np.flatnonzero(hist >= hist.max() * .22))
            bass = [n for n in active if n.instrument in ('electric_bass', 'acoustic_bass') or n.source == 'bass']
            roots = {n.pitch % 12 for n in bass}
            candidates = [(r, q, {r, (r+t) % 12, (r+7) % 12})
                          for r in roots for q, t in (('major', 4), ('minor', 3))
                          if r in pcs and (r+t) % 12 in pcs and pcs <= {r, (r+t) % 12, (r+7) % 12}]
            if len(candidates) == 1:
                root, quality, pitches = candidates[0]
                chord = dict(root=root, quality=quality, pitches=sorted(pitches), confidence=None)
        candidates = [dict(chord, fit=chord['confidence'] or .9)] if chord else []
        if not candidates and combined.max() > 0:
            # Continuity may resolve a close fit, but every candidate still
            # needs all three observed tones. No key-based harmonization.
            x = np.maximum(0, combined-np.percentile(combined, 25))
            for root in range(12):
                for quality, third in (('major', 4), ('minor', 3)):
                    pcs = [root, (root+third) % 12, (root+7) % 12]
                    fit = float(x[pcs].sum()/max(np.linalg.norm(x)*np.sqrt(3), 1e-9))
                    support = x[pcs].sum()/max(x.sum(), 1e-9)
                    if fit >= .78 and support >= .7 and min(x[pcs]) >= .22*x.max():
                        candidates.append(dict(root=root, quality=quality, pitches=pcs,
                                               confidence=round(fit, 4), fit=fit))
            candidates.sort(key=lambda c: (-c['fit'], c['root'], c['quality']))
        for left, right in allowed_parts(start, end, blocked):
            if right-left >= 80:
                observations.append(dict(candidates=candidates, decisive=bool(chord),
                    evidence=dict(start_ms=left, end_ms=right, reliable_beats=bool(reliable),
                                  evidence_ids=sorted({str(n.event_id) for n in active}),
                                  acoustic_evidence=bool(acoustic))))
    chords = chord_path(observations)
    resolved = {(c['start_ms'], c['end_ms']) for c in chords}
    for w in observations:
        e = w['evidence']
        if (e['start_ms'], e['end_ms']) not in resolved and e['evidence_ids']:
            reviews.append(dict(start_ms=e['start_ms'], end_ms=e['end_ms'],
                                reason='和声证据不足，仅保留可演奏的原始支撑音'))
    return chords, reviews, reliable


def merge_harmony(chords):
    """One harmonic span, not a new complete voicing at every beat."""
    spans = []
    for chord in chords:
        if (spans and (spans[-1]['root'], spans[-1]['quality']) == (chord['root'], chord['quality'])
                and abs(spans[-1]['end_ms']-chord['start_ms']) < 1e-3):
            spans[-1]['end_ms'] = chord['end_ms']
            spans[-1]['evidence_ids'] = sorted(set(spans[-1]['evidence_ids']) | set(chord['evidence_ids']))
            spans[-1]['acoustic_evidence'] |= chord['acoustic_evidence']
            spans[-1]['continuity_resolved'] |= chord.get('continuity_resolved', False)
        else:
            spans.append(dict(chord))
    return spans


def sounding_end(note):
    # Both held keys and strong acoustic tails matter; releasing a piano key
    # cannot silence its sample. Sustain metadata includes the release envelope.
    return note.time_ms + max(note.duration_ms, note.extra.get('strongDuration', 0))


def below_lead(key, melody):
    """A support voice may never be rescued by moving it above the melody."""
    return all(SKY_MIDI[m.key]-SKY_MIDI[key] >= 3 and
               (SKY_MIDI[m.key]-SKY_MIDI[key]) % 12 not in (0, 1, 6, 11)
               for m in melody)


def melody_regions(lead, beat):
    """Include short breaths, but leave independent instrumental passages free."""
    regions = []
    for note in lead:
        end = note.time_ms+note.duration_ms
        if regions and note.time_ms-regions[-1][1] <= max(700., 1.5*beat):
            regions[-1][1] = max(regions[-1][1], end)
        else:
            regions.append([note.time_ms, end])
    return regions


def synchronize_attacks(candidates, lead, pulse, blocked, chord_at, minimum):
    """Align support to the final lead, never move the lead or cross harmony/rests.

    Only nearby attacks can move (half a local beat, at most 240 ms). Support
    within a held melody or a short breath needs an anchor; long independent
    passages keep their source rhythm. Original events remain the provenance.
    """
    starts = sorted({n.time_ms for n in lead})
    regions = melody_regions(lead, pulse.fallback_ms)
    region_starts = [a for a, _b in regions]
    aligned, origins, rejected = [], {}, Counter()

    def identity(chord):
        return (chord['root'], chord['quality']) if chord else None

    for note, chord in candidates:
        t = note.start_ms
        _, beat, _ = pulse.at(t)
        reach = min(240., .5*beat)
        anchors = starts[bisect_left(starts, t-reach):bisect_right(starts, t+reach)]
        index = bisect_right(region_starts, t)-1
        needs_anchor = bool(anchors) or (index >= 0 and t < regions[index][1])
        if not needs_anchor:
            aligned.append((note, chord))
            origins[id(note)] = (note, 'independent')
            continue
        chosen = None
        for anchor in sorted(anchors, key=lambda a: (abs(a-t), a)):
            if identity(chord_at(anchor)) != identity(chord):
                continue
            if any(a <= anchor < b or (a < max(anchor, t)-1e-3 and b > min(anchor, t)+1e-3)
                   for a, b in blocked):
                continue
            duration = min(note.duration_ms, t+note.duration_ms-anchor)
            if chord:
                duration = min(duration, chord['end_ms']-anchor)
            if duration >= minimum-1e-3:
                chosen = replace(note, start_ms=anchor, duration_ms=duration)
                break
        if chosen is None:
            rejected['no_safe_melody_anchor'] += 1
            continue
        aligned.append((chosen, chord))
        origins[id(chosen)] = (note, 'melody')
    return aligned, origins, rejected


def rich_accompaniment(score, result, minimum=80, overrides=()):
    meta = score.root['transcription']
    instrument, shift = meta['instrument'], meta['shift']
    sustained = instrument in SUSTAIN
    lead = list(score.notes)
    held = []
    for note in lead:
        held = [end for end in held if end > note.time_ms+1e-3]
        held.append(note.time_ms+note.duration_ms)
        if len(held) > MAXIMUM_KEYS:
            raise ScoreError('主旋律本身超过三键同时发声，请选择单独的主旋律 MIDI 音轨后重新编配。')
    lead_starts = [n.time_ms for n in lead]
    lead_ends = np.maximum.accumulate([n.time_ms+n.duration_ms for n in lead]).tolist()

    def lead_during(start, end):
        return [n for n in lead[bisect_right(lead_ends, start+1e-3):bisect_left(lead_starts, end-1e-3)]
                if n.time_ms+n.duration_ms > start+1e-3]

    blocked = exclusions(result, overrides)
    evidence = evidence_notes(result, max(80, minimum))
    evidence = [replace(n, start_ms=a, duration_ms=b-a) for n in evidence
                for a, b in allowed_parts(n.start_ms, n.start_ms+n.duration_ms, blocked) if b-a >= minimum]
    windows, reviews, reliable = harmony_windows(result, evidence, blocked)
    chords = merge_harmony(windows)
    chord_starts = [c['start_ms'] for c in chords]

    def chord_at(start):
        i = bisect_right(chord_starts, start+1e-3)-1
        return chords[i] if i >= 0 and start < chords[i]['end_ms']-1e-3 else None

    beat = 60000 / score.bpm
    beats = np.asarray(getattr(result, 'analysis', {}).get('beats_ms', []))
    if reliable:
        beat = float(np.median(np.diff(beats)))
    pulse = pulse_from_evidence(getattr(result, 'analysis', {}), evidence, beat)
    phrases = phrase_boundaries(lead, blocked, beat)
    selected = {(n.extra.get('sourceTime'), n.extra.get('sourcePitch'), n.extra.get('modelInstrument', ''))
                for n in lead}
    candidates = []
    for n in evidence:
        if (n.start_ms, n.pitch, n.instrument) in selected or any(
                abs(n.start_ms-m.extra['sourceTime']) < 40 and n.pitch == m.extra['sourcePitch']
                for m in lead[max(0, bisect_left(lead_starts, n.start_ms)-2):
                              bisect_left(lead_starts, n.start_ms+40)+1]):
            continue
        candidates.append((n, chord_at(n.start_ms)))
    # Completion is offered once per harmonic span, preferably on an observed
    # attack. Original evidence and completion then share the same voice budget.
    evidence_starts = sorted({n.start_ms for n in evidence})
    for chord in chords:
        a, b = chord['start_ms'], chord['end_ms']
        i = bisect_left(evidence_starts, a-1e-3)
        onset = evidence_starts[i] if i < len(evidence_starts) and evidence_starts[i] < min(b, a+beat) else None
        if onset is None:
            if not reliable:
                continue
            onset = a
        for pc in chord['pitches']:
            n = MelodyNote(48+pc, onset, min(b-onset, beat*1.5), None,
                           role='accompaniment', source='harmony', generated=True)
            if n.duration_ms >= minimum:
                candidates.append((n, chord))
    candidate_count = len(candidates)
    candidates, origins, alignment_rejected = synchronize_attacks(
        candidates, lead, pulse, blocked, chord_at, minimum)
    candidates.sort(key=lambda c: (c[0].start_ms, c[0].generated, c[0].pitch))
    groups = []
    for candidate in candidates:
        tolerance = (40 if groups and origins[id(candidate[0])][1] == 'independent' and
                     origins[id(groups[-1][0][0])][1] == 'independent' else 1e-3)
        if not groups or abs(candidate[0].start_ms-groups[-1][0][0].start_ms) > tolerance:
            groups.append([])
        groups[-1].append(candidate)

    tails = strong_tails(instrument)
    envelopes = sample_energy(instrument)
    lookback = max(2000., max(e[0][-1] for e in envelopes))
    contexts, placements = [], {}
    layers = {'bass': [], 'inner': []}
    for index, group in enumerate(groups):
        start = group[0][0].start_ms
        accent, local_beat, strength = pulse.at(start)
        nearby = lead_during(start-.75*local_beat, start+1.5*local_beat)
        instrumental = not any(n.extra.get('source') == 'vocals' for n in nearby)
        busy = bisect_left(lead_starts, start+local_beat)-bisect_left(lead_starts, start-local_beat) >= 5
        chord = chord_at(start)
        chord_id = (chord['root'], chord['quality']) if chord else None
        phrase = bisect_right(phrases, start)-1
        harmonic_attack = chord and start-chord['start_ms'] < min(160., .35*local_beat)
        weak = accent in ('pulse', 'offbeat') and pulse.accent_confidence >= .55 and not harmonic_attack
        first_role = 'inner' if weak and nearby and chord else 'bass'

        def ranking(candidate, voice):
            n, c = candidate
            bass = n.instrument in ('acoustic_bass', 'electric_bass') or n.source == 'bass'
            harmonic = (0 if n.pitch % 12 == c['root'] else
                        1 if n.pitch % 12 == (c['root']+(3 if c['quality'] == 'minor' else 4)) % 12
                        else 2) if c else 2
            preference = (not bass, harmonic) if voice == 'bass' else (bass, 0 if harmonic == 1 else 1)
            return (*preference, n.generated, -min(n.duration_ms, local_beat*2), n.pitch)

        group.sort(key=lambda item: ranking(item, first_role))
        for n, c in group:
            for key, pitch in enumerate(SKY_MIDI):
                if pitch % 12 != (n.pitch+shift) % 12:
                    continue
                a = n.start_ms
                end = min(a+n.duration_ms, a+local_beat*(2 if instrumental else 1.25))
                if c:
                    end = min(end, c['end_ms'])
                release = 1500/44.1 if sustained else 0.
                if sustained:
                    for left, right in blocked:
                        if left < end+release and right > a:
                            end = min(end, left-release)
                    for m in lead_during(a, end+release):
                        if not below_lead(key, [m]):
                            end = min(end, m.time_ms-release)
                if end-a < minimum-1e-3:
                    continue
                strong_duration = end-a+release if sustained else tails[key]
                horizon = a+max(end-a, strong_duration)
                if any(left < horizon-1e-3 and right > a+1e-3 for left, right in blocked):
                    continue
                active_lead = lead_during(a, horizon)
                if not below_lead(key, active_lead or nearby) or (not active_lead and pitch > 72):
                    continue
                placements[index, id(n), key] = (end, strong_duration, horizon, active_lead)
        for voice in ('bass', 'inner'):
            options = {}
            for rank, (n, _c) in enumerate(sorted(group, key=lambda item: ranking(item, voice))):
                for key, pitch in enumerate(SKY_MIDI):
                    if (index, id(n), key) not in placements:
                        continue
                    cost = .22*min(rank, 8)+.035*abs(pitch-(62 if voice == 'bass' else 67))
                    options[pitch] = min(cost, options.get(pitch, float('inf')))
            if options:
                layers[voice].append((index, phrase, start, local_beat, sorted(options.items())))
        contexts.append((start, accent, strength, local_beat, nearby, instrumental, busy,
                         chord, chord_id, phrase, first_role))
    plans = {role: voice_path(rows, 62 if role == 'bass' else 67) for role, rows in layers.items()}
    kept, recent, rejected, patterns = [], [], alignment_rejected, Counter()
    energy_recent = []
    previous = {}  # Independent bass/inner paths; an upper tone cannot lift the next bass.
    last_start, last_pcs, last_chord, last_phrase = -1e9, set(), None, None
    for index, group in enumerate(groups):
        (start, accent, strength, beat, nearby, instrumental, busy,
         chord, chord_id, phrase, first_role) = contexts[index]
        if phrase != last_phrase or start-last_start > max(1800., 3*beat):
            previous.clear()
            last_pcs, last_chord = set(), None
        changing = chord_id is not None and chord_id != last_chord
        spacing = beat * (.55 if instrumental else .9 if busy or sustained else .75)
        if start-last_start < (min(160, spacing) if changing else spacing)-1e-3:
            rejected['attack_spacing'] += len(group)
            continue
        # Missing/ambiguous melody is allowed independent support, with a low
        # register reserved across short breaths. Three keys include the lead.
        budget = 1 if busy or (sustained and not instrumental) else 2
        if not nearby and chord:
            budget = 3 if not sustained else 2
        group_budget = budget if chord and first_role == 'bass' else 1
        planned = plans[first_role].get(index)
        # Keep the phrase plan advisory: a failed candidate can fall back to
        # another supported voice, but never bypass the safety checks below.
        if planned is not None:
            group.sort(key=lambda item: (item[0].pitch+shift) % 12 != planned % 12)
        pcs, admitted, group_keys = set(), 0, []
        recent = [p for p in recent if sounding_end(p) > start+1e-3]
        energy_recent = [p for p in energy_recent if p.time_ms+max(lookback, p.duration_ms+35) > start]
        for n, c in group:
            pc = (n.pitch+shift) % 12
            if admitted >= group_budget or pc in pcs:
                rejected['redundant_voice'] += 1
                continue
            # Non-diatonic support is omitted rather than turned into a different
            # chord by independent nearest-white-key mapping. The lead is intact.
            options = [k for k, pitch in enumerate(SKY_MIDI) if pitch % 12 == pc]
            if not options:
                rejected['unplayable_pitch_class'] += 1
                continue
            if pc in last_pcs and not changing and start-last_start < beat*(.85 if instrumental else 1.25):
                rejected['repeated_support'] += 1
                continue
            role = first_role if admitted == 0 else 'inner'
            target = previous.get(role, 62 if role == 'bass' else 67)
            ceiling = min((SKY_MIDI[m.key] for m in nearby), default=76)
            planned_key = plans[role].get(index, target)
            options.sort(key=lambda k: (.6*abs(SKY_MIDI[k]-target) + 1.4*abs(SKY_MIDI[k]-planned_key) +
                                        max(0, 5-(ceiling-SKY_MIDI[k]))*2, SKY_MIDI[k]))
            chosen = None
            for key in options:
                if group_keys and SKY_MIDI[key]-SKY_MIDI[group_keys[0]] < 3:
                    continue
                a = n.start_ms
                placement = placements.get((index, id(n), key))
                if placement is None:
                    continue
                end, strong_duration, horizon, active_lead = placement
                relevant = [p for p in recent if sounding_end(p) > a+1e-3]
                if any(p.key == key or
                       abs(SKY_MIDI[p.key]-SKY_MIDI[key]) % 12 in (0, 1, 2, 6, 10, 11)
                       for p in relevant):
                    continue
                # Strong tails consume voices even after note-off. Check future
                # melody onsets too, so an instrumental fill cannot crowd re-entry.
                points = {a, *(m.time_ms for m in active_lead if a < m.time_ms < horizon)}
                crowded = False
                for t in points:
                    voices = [p for p in relevant if sounding_end(p) > t+1e-3]
                    melody = lead_during(t, t+.01)
                    limit = min(budget, 2 if melody else 3)
                    if len(voices)+1 > limit or len(voices)+1+len(melody) > MAXIMUM_KEYS:
                        crowded = True
                        break
                if crowded:
                    continue
                # The same key press has no controllable velocity in the game.
                # Admit an attack only when its actual sample power fits under
                # the melody, including a piano lead that has already decayed.
                energy_ratio = 0.
                for t in points:
                    # Assess a short phrase, not each transient in isolation:
                    # a bass attack can support several upcoming melody notes.
                    until = t+max(320, min(2*beat, 1600))
                    if not lead_during(t, until):
                        continue
                    melodic_energy = sum(interval_energy(envelopes[m.key], m.time_ms, m.duration_ms, t, until)
                                         for m in lead_during(t-lookback, until))
                    support_energy = interval_energy(envelopes[key], a, end-a, t, until)
                    support_energy += sum(interval_energy(envelopes[p.key], p.time_ms, p.duration_ms, t, until)
                                          for p in energy_recent)
                    ratio = support_energy / max(melodic_energy, 1e-12)
                    energy_ratio = max(energy_ratio, ratio)
                if energy_ratio > (1.0 if instrumental else .7):
                    rejected['melody_energy_budget'] += 1
                    continue
                chosen = key, end, strong_duration, energy_ratio
                break
            if chosen is None:
                rejected['register_harmony_or_tails'] += 1
                continue
            key, end, strong_duration, energy_ratio = chosen
            pattern = ('bass' if budget == 1 else 'block' if admitted else
                       'arpeggio' if reliable and chord_id is not None and chord_id == last_chord
                       and pc not in last_pcs else 'source')
            origin, alignment = origins[id(n)]
            extra = dict(role='accompaniment', source=n.source, sourcePitch=n.pitch, sourceTime=origin.start_ms,
                         sourceDuration=origin.duration_ms, generated=n.generated, pitchConfidence=n.confidence,
                         attackAlignment=alignment, alignmentOffsetMs=n.start_ms-origin.start_ms,
                         approximated=False, folded=SKY_MIDI[key] != n.pitch+shift,
                         arrangementPolicy=POLICY, pattern=pattern, voice=role,
                         balanceContext='instrumental' if instrumental else 'vocal',
                         maximumSupport=budget, strongDuration=strong_duration,
                         phraseId=phrase, rhythmicRole=accent, accentStrength=strength,
                         plannedVoicePitch=planned_key,
                         estimatedEnergyRatio=energy_ratio)
            if n.event_id is not None:
                extra.update(modelEvent=n.event_id, modelInstrument=n.instrument, provider=n.provider,
                             audioSource=n.audio_source)
            if c:
                extra.update(harmonyRoot=c['root'], harmonyQuality=c['quality'],
                             harmonySpan=[c['start_ms'], c['end_ms']], evidenceEvents=c['evidence_ids'],
                             acousticEvidence=c['acoustic_evidence'],
                             arrangementEvent=f'{POLICY}:{n.start_ms:.3f}:{n.pitch}:{pattern}')
            kept.append(Note(key, n.start_ms, end-n.start_ms, extra))
            recent.append(kept[-1])
            energy_recent.append(kept[-1])
            previous[role] = SKY_MIDI[key]
            pcs.add(pc)
            group_keys.append(key)
            admitted += 1
            patterns[pattern] += 1
        if admitted:
            last_start, last_pcs, last_chord = start, pcs, chord_id
            last_phrase = phrase
    score.notes = sorted(lead+kept, key=lambda n: (n.time_ms, n.key))
    meta.update(chords=chords, harmony_review=reviews, harmony_rhythm=pulse.metadata(),
                harmony_phrases=phrases, harmony_alignment=dict(policy='final-melody-onsets',
                    maximum_shift_ms=240, maximum_shift_beats=.5,
                    melody_regions_ms=melody_regions(lead, pulse.fallback_ms), independent_passages='source-rhythm'),
                clarity=dict(policy=POLICY, maximum_polyphony=MAXIMUM_KEYS, maximum_support_with_lead=2,
                             minimum_lead_gap_semitones=3, strong_tails_counted=True,
                             harmony_windows=len(windows), harmonic_spans=len(chords),
                             candidates=candidate_count, aligned_candidates=len(candidates), kept=len(kept),
                             generated=sum(n.extra['generated'] for n in kept),
                             rejected=dict(rejected), patterns=dict(patterns)))
    if reviews:
        meta['warnings'].append('部分和声证据不足，仅选取原始支撑音；可在待核对信息中查看。')
    if not kept:
        meta['warnings'].append('当前音域或和声证据不足，未添加伴奏。')
    return score
