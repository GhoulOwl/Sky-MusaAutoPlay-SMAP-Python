"""Conservative, equal-loudness accompaniment for actual Sky instrument samples."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from functools import lru_cache

import numpy as np
import soundfile as sf

from ..audio import ASSETS, INSTRUMENTS
from ..midi import SKY_MIDI


def generation_mode(mode, instrument):
    if instrument not in INSTRUMENTS or mode not in ('auto', 'simple', 'melody'):
        raise ValueError('生成模式或音色无效')
    return ('melody' if instrument == 'Violin' else 'simple') if mode == 'auto' else mode


@lru_cache(maxsize=4)
def piano_tails(threshold_db=-40):
    """Last 20 ms RMS block above a relative sample-envelope threshold.

    This estimates masking risk; it never truncates audio or changes gain.
    Read the bundled, immutable samples once, not on every remapping.
    """
    tails = []
    for key in range(15):
        audio, rate = sf.read(ASSETS / 'instruments' / 'Piano' / f'{key}.wav', always_2d=True)
        hop = max(1, round(rate * .02))
        rms = np.array([np.sqrt(np.mean(audio[i:i + hop] ** 2)) for i in range(0, len(audio), hop)])
        audible = np.flatnonzero(rms > rms.max() * 10 ** (threshold_db / 20))
        tails.append(min(len(audio), (audible[-1] + 1) * hop) / rate * 1000 if len(audible) else 0)
    return tuple(tails)


def sparse_accompaniment(score, instrument):
    """Pick one evidence-backed support tone, accounting for its audible tail.

    Reject rather than lower its volume: Windows key playback has no velocity.
    Only accompaniment changes here. Raw timing and melody events stay intact.
    """
    meta = score.root['transcription']
    melody = sorted((n for n in score.notes if n.extra.get('role') != 'accompaniment'), key=lambda n: n.time_ms)
    starts = [n.time_ms for n in melody]
    ends = np.maximum.accumulate([n.time_ms + n.duration_ms for n in melody]).tolist()
    beats = np.asarray(meta.get('beats_ms', ()))
    beat = float(np.median(np.diff(beats))) if len(beats) >= 4 else 60000 / score.bpm
    tails = piano_tails() if instrument == 'Piano' else (0,) * 15
    strong_tails = piano_tails(-18) if instrument == 'Piano' else (0,) * 15
    # Violin release is sample-clock based, currently 1500 samples at 44.1 kHz.
    release = 1500 / 44.1 if instrument == 'Violin' else 0
    chord_starts = [c['start_ms'] for c in meta.get('chords', [])]
    chords = meta.get('chords', [])
    def rank(note):
        index = bisect_right(chord_starts, note.extra['sourceTime']) - 1
        root = chords[index]['root'] if index >= 0 else note.extra['sourcePitch'] % 12
        return note.time_ms, (note.extra['sourcePitch'] % 12 != root), note.extra['sourcePitch']
    candidates = sorted((n for n in score.notes if n.extra.get('role') == 'accompaniment'), key=rank)
    accepted, reasons, sounding_until = [], Counter(), -float('inf')
    for note in candidates:
        start = note.time_ms
        if start < sounding_until - 1e-6:
            reasons['previous_tail'] += 1
            continue
        if accepted:
            same_pitch_class = (note.extra['sourcePitch'] - accepted[-1].extra['sourcePitch']) % 12 == 0
            spacing = (4 if same_pitch_class else 2) * beat
            if start - accepted[-1].time_ms < spacing - 1e-6:
                reasons['repeated_support'] += 1
                continue
        density_left = bisect_left(starts, start - .25 * beat)
        density_right = bisect_right(starts, start + 1.75 * beat)
        if density_right - density_left >= (4 if instrument == 'Violin' else 5):
            reasons['busy_melody'] += 1
            continue
        raw = note.extra['sourcePitch'] + meta['shift']
        options = []
        for key, pitch in enumerate(SKY_MIDI):
            if pitch % 12 != raw % 12:
                continue
            audible_end = start + max(note.duration_ms + release, tails[key])
            # Quiet decay still excludes another support attack, but should not
            # veto a later melody passing tone. Check harmonic collisions over
            # the stronger envelope; violin sustains through the full interval.
            strong_end = start + (strong_tails[key] if instrument == 'Piano' else note.duration_ms + release)
            left, right = bisect_right(ends, start), bisect_left(starts, strong_end)
            lead = [n for n in melody[left:right] if n.time_ms + n.duration_ms > start]
            if not lead:
                continue
            intervals = [SKY_MIDI[n.key] - pitch for n in lead]
            # Leave at least a fourth of separation in the stronger envelope.
            if min(intervals) < 5 or any(d % 12 in (1, 2, 6, 10, 11) for d in intervals):
                continue
            target = accepted[-1].key if accepted else max(0, min(n.key for n in lead) - 5)
            options.append((abs(key - target), key, audible_end))
        if not options:
            reasons['range_or_harmony'] += 1
            continue
        # Candidates at the same time may be root and fifth. Prefer the root
        # when it is viable; candidates are otherwise ordered deterministically.
        _, key, audible_end = min(options)
        chord_index = bisect_right(chord_starts, note.extra['sourceTime']) - 1
        if note.extra.get('generated') and chord_index >= 0:
            chord = chords[chord_index]
            note.extra['harmonyRoot'] = chord['root']
        note.key = key
        note.extra['folded'] = SKY_MIDI[key] != raw
        note.extra['audibleDuration'] = audible_end - start
        accepted.append(note)
        sounding_until = audible_end
    meta['clarity'] = dict(instrument=instrument, maximum_accompaniment=1,
                           candidates=len(candidates), kept=len(accepted), rejected=dict(reasons),
                           tail_threshold_db=-40 if instrument == 'Piano' else None,
                           harmony_threshold_db=-18 if instrument == 'Piano' else None)
    score.notes = sorted(melody + accepted, key=lambda n: (n.time_ms, n.key))
    return score
