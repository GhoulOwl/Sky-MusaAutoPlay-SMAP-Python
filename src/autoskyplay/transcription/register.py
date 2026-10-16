"""Choose key paths for complete phrases, preserving contour across range limits."""
from __future__ import annotations

import numpy as np

from ..midi import SKY_MIDI, WHITE


def melody_phrases(notes):
    phrases, phrase, last = [], [], None
    for index, note in enumerate(notes):
        if note.role != 'melody':
            continue
        if last is not None and (note.start_ms - last.start_ms - last.duration_ms > 600 or note.source != last.source):
            phrases.append(phrase)
            phrase = []
        phrase.append((index, note))
        last = note
    if phrase:
        phrases.append(phrase)
    return phrases


def melody_keys(notes, shift, *, with_cost=False, anchors=None):
    """Preserve phrase contour before proximity to an unreachable raw octave.

    All source notes remain. Repeated complete pitch sequences reuse one key
    path; a different duration or accompaniment cannot flip their register.
    """
    result, total, repeated = {}, 0., {}
    pitches = np.asarray(SKY_MIDI)
    jumps = pitches[None, :] - pitches[:, None]
    phrases = melody_phrases(notes)
    for phrase in phrases:
        costs, parents = [], []
        previous_raw = None
        signature = (phrase[0][1].source, tuple(n.pitch for _, n in phrase))
        repeat = repeated.get(signature) if len(phrase) >= 4 and not anchors else None
        for _, note in phrase:
            raw = note.pitch + shift
            pcs = {raw % 12} if raw % 12 in WHITE else {(raw - 1) % 12, (raw + 1) % 12}
            weight = np.clip(np.sqrt(note.duration_ms / 250), .6, 2)
            local = abs(pitches - raw) * .04 * weight
            local += abs(pitches - 72) * .025  # room around both ends of the keyboard
            local = np.where(np.isin(pitches % 12, list(pcs)), local, 1e9)
            anchor = (anchors or {}).get((note.start_ms, note.pitch, note.instrument))
            if anchor is not None:
                local = np.where(np.arange(15) == anchor, 0., 1e12)
            if repeat is not None:
                local = np.where(np.arange(15) == repeat[len(costs)], local, 1e9)
            if previous_raw is None:
                costs.append(local)
                parents.append(np.zeros(15, dtype=int))
            else:
                interval = raw - previous_raw
                penalty = abs(jumps - interval) * .8
                if interval:
                    penalty += (jumps * interval < 0) * 24
                    penalty += (jumps == 0) * 8  # don't erase an available step
                    if abs(interval) <= 5:
                        penalty += (abs(jumps) >= 10) * 48
                else:
                    # Rearticulating a held pitch is never a register-change cue.
                    penalty = np.where(jumps == 0, 0, 1e9)
                matrix = costs[-1][:, None] + penalty
                parents.append(np.argmin(matrix, axis=0))
                costs.append(local + np.min(matrix, axis=0))
            previous_raw = raw
        key = int(np.argmin(costs[-1]))
        total += float(costs[-1][key])
        for i in range(len(phrase) - 1, -1, -1):
            result[phrase[i][0]] = key
            key = int(parents[i][key])
        if len(phrase) >= 4:
            repeated.setdefault(signature, [result[i] for i, _ in phrase])
    return (result, total) if with_cost else result


def phrase_shift(notes):
    """Keep the best duration-weighted diatonic fit, then choose a phrase path.

    Accompaniment does not vote on the melody's key or octave. It must adapt to
    the selected lead, so switching its visibility cannot move the melody.
    """
    lead = [n for n in notes if n.role == 'melody'] or list(notes)
    if not lead:
        return 0
    fits = {s: sum(n.duration_ms for n in lead if (n.pitch + s) % 12 not in WHITE)
            for s in range(-36, 37)}
    best = min(fits.values())
    candidates = [s for s in fits if fits[s] <= best + 1e-6]
    def cost(shift):
        _, path_cost = melody_keys(lead, shift, with_cost=True)
        outside = sum(n.duration_ms for n in lead if not 60 <= n.pitch + shift <= 84)
        return path_cost, outside, abs(shift), shift
    return min(candidates, key=cost)
