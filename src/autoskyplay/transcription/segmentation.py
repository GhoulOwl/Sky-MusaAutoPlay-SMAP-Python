"""Conservative contour decoding; never synthesizes notes in genuine silence."""
from __future__ import annotations

import numpy as np


def runs(values):
    edges = np.r_[0, np.flatnonzero(values[1:] != values[:-1]) + 1, len(values)]
    return [(int(a), int(b), values[a]) for a, b in zip(edges[:-1], edges[1:]) if b > a]


def onset_decision(result, midi, attack, start, end, threshold):
    """Veto only well-supported continuous tones INSIDE a source-pitch run.

    Never joins separated notes, rests, source changes or mapped Sky keys.
    Weak syllables can keep flat RMS: require independent spectral continuity.
    Missing evidence/edge context/possible articulation keeps the old boundary.
    Thresholds are conservative engineering guards, not calibrated probabilities.
    """
    radius = max(1, int(np.ceil(60 / result.hop_ms)))
    left, right = attack - radius, attack + radius + 1
    if left < start or right > end:
        return 'keep-edge'
    attacks = np.flatnonzero(result.onset[start:end] >= .5) + start
    position = np.searchsorted(attacks, attack)
    # A regular same-pitch pulse train may be repeated piano/guitar notes under
    # a sustaining pedal. Do not flatten its rhythm on weak negative evidence.
    for i in range(max(0, position - 2), min(position + 1, len(attacks) - 2)):
        intervals = np.diff(attacks[i:i + 3]) * result.hop_ms
        if intervals.min() >= 80 and intervals.max() <= 1500 and intervals.max() <= intervals.min() * 1.2:
            return 'keep-repeated-pulse'
    confidence = result.confidence[left:right]
    if confidence.min() < max(.8, threshold) or np.ptp(confidence) >= .15:
        return 'keep-confidence-change'
    if np.ptp(midi[left:right]) >= .5:
        return 'keep-pitch-change'
    energy = result.energy_db[left:right]
    if np.ptp(energy) >= 3:
        return 'keep-energy-change'
    change = result.analysis.get('boundary_change')
    if change is None or change[attack] < 0:
        return 'keep-missing-evidence'
    if change[attack] >= .01:
        return 'keep-spectral-change'
    return 'sustain'


def decode(result, threshold, minimum_ms, ornaments='simplify', *, audit=None):
    from .melody import MelodyNote

    result.validate()
    if not 0 <= threshold <= 1 or not 20 <= minimum_ms <= 1000 or ornaments not in ('simplify', 'keep'):
        raise ValueError('音符过滤参数无效')
    midi = 69 + 12 * np.log2(np.maximum(result.frequency, 1) / 440)
    stable = np.median(np.lib.stride_tricks.sliding_window_view(np.pad(midi, (2, 2), mode='edge'), 5), axis=1)
    pitches = np.rint(stable).astype(int)
    # Hysteresis may continue an established tone, but cannot start one from noise.
    floor = max(-65, float(np.percentile(result.energy_db, 90)) - 48)
    audible = (result.energy_db >= floor) & (result.frequency > 0) & (pitches >= 0) & (pitches <= 127)
    low = audible & (result.confidence >= threshold * .65)
    high = audible & (result.confidence >= threshold)
    voiced = np.zeros(len(low), dtype=bool)
    for a, b, active in runs(low):
        if active:
            seeds = np.flatnonzero(high[a:b])
            if len(seeds):
                voiced[a + seeds[0]:b] = True
    # Repair brief confidence dropouts only with pitch, energy and no reattack evidence.
    for a, b, active in runs(voiced):
        if (not active and a > 0 and b < len(voiced) and (b - a) * result.hop_ms <= 40
                and pitches[a - 1] == pitches[b] and audible[a:b].all()
                and not np.any(result.onset[a:b + 1] >= .5)
                and np.min(result.energy_db[a:b]) >= min(result.energy_db[a - 1], result.energy_db[b]) - 10):
            voiced[a:b] = True
            pitches[a:b] = pitches[a - 1]
    labels = np.where(voiced, pitches, -1)
    # Suppress short returns around a stable tone, not lasting leaps or articulated ornaments.
    limit = min(80, minimum_ms) if ornaments == 'simplify' else 30
    original = labels.copy()
    for a, b, pitch in runs(original):
        if (pitch >= 0 and a > 0 and b < len(labels) and (b - a) * result.hop_ms < limit
                and original[a - 1] == original[b] and original[b] >= 0
                and abs(pitch - original[b]) in (1, 12)
                and not np.any(result.onset[a:b] >= .5)):
            labels[a:b] = original[b]
    notes = []
    for a, b, pitch in runs(labels):
        if pitch < 0:
            continue
        boundaries = [a]
        for attack in np.flatnonzero(result.onset[a:b] >= .5) + a:
            if (attack - boundaries[-1]) * result.hop_ms >= minimum_ms:
                decision = onset_decision(result, midi, attack, a, b, threshold)
                if audit is not None:
                    audit.append(dict(time_ms=float(attack * result.hop_ms), pitch=int(pitch),
                                      decision=decision, run_start_ms=float(a * result.hop_ms),
                                      run_end_ms=float(b * result.hop_ms)))
                if decision != 'sustain':
                    boundaries.append(int(attack))
        boundaries.append(b)
        for start, end in zip(boundaries[:-1], boundaries[1:]):
            start_ms = start * result.hop_ms
            duration = min(end * result.hop_ms, result.duration_ms) - start_ms
            if duration >= minimum_ms:
                notes.append(MelodyNote(int(pitch), start_ms, duration,
                                        float(np.mean(result.confidence[start:end]))))
    return notes
