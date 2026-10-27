"""Evidence-only harmonic continuity, pulse accents and phrase voice leading.

These are arrangement hints. They cannot override the final register, silence,
tail or energy constraints, and do not change the melody or source clock.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

import numpy as np


def chord_path(windows):
    """Resolve ambiguous supported triads next to decisive observations.

    Empty evidence and discontinuities split the path. A transition can choose
    among locally supported states, never carry a chord into an unknown window.
    """
    output, chain = [], []

    def flush():
        if not chain:
            return
        if not any(w['decisive'] for w in chain):
            chain.clear()
            return
        scores, links = [], []
        for i, window in enumerate(chain):
            row, back = [], []
            for chord in window['candidates']:
                value = chord['fit']
                if not i:
                    row.append(value)
                    back.append(-1)
                    continue
                alternatives = []
                for j, previous in enumerate(chain[i-1]['candidates']):
                    same = (previous['root'], previous['quality']) == (chord['root'], chord['quality'])
                    common = len(set(previous['pitches']) & set(chord['pitches']))
                    # A small continuity preference, below a decisive local margin.
                    transition = .045 if same else .008*common-.02
                    alternatives.append(scores[-1][j]+transition)
                best = max(range(len(alternatives)), key=alternatives.__getitem__)
                row.append(value+alternatives[best])
                back.append(best)
            scores.append(row)
            links.append(back)
        selected = max(range(len(scores[-1])), key=scores[-1].__getitem__)
        path = []
        for i in range(len(chain)-1, -1, -1):
            w = chain[i]
            chord = w['candidates'][selected]
            path.append(dict(w['evidence'], **{k: v for k, v in chord.items() if k != 'fit'},
                             continuity_resolved=not w['decisive']))
            selected = links[i][selected]
        output.extend(reversed(path))
        chain.clear()

    for window in windows:
        if chain and abs(chain[-1]['evidence']['end_ms']-window['evidence']['start_ms']) > 1e-3:
            flush()
        if not window['candidates']:
            flush()
        else:
            chain.append(window)
    flush()
    return output


@dataclass(frozen=True)
class Pulse:
    beats: tuple
    accents: tuple
    fallback_ms: float
    confidence: float
    accent_confidence: float

    def at(self, time):
        if len(self.beats) < 4 or self.confidence < .55:
            return 'source', self.fallback_ms, 0.
        i = max(0, min(len(self.beats)-2, bisect_right(self.beats, time)-1))
        period = self.beats[i+1]-self.beats[i]
        # Do not extend a tracked grid into an untracked intro or outro.
        if time < self.beats[0]-.2*period or time > self.beats[-1]+.2*period:
            return 'source', self.fallback_ms, 0.
        j = min((i, i+1), key=lambda k: abs(self.beats[k]-time))
        distance = abs(self.beats[j]-time)
        if distance > min(100., .22*period):
            return 'offbeat', period, 0.
        accent = self.accents[j]
        label = 'strong' if self.accent_confidence >= .55 and accent >= .65 else 'pulse'
        return label, period, accent

    def metadata(self):
        return dict(method='source-onset-accents', beat_confidence=self.confidence,
                    accent_confidence=round(self.accent_confidence, 4),
                    strong_beats_ms=[t for t, a in zip(self.beats, self.accents)
                                     if self.accent_confidence >= .55 and a >= .65],
                    inferred_meter=None)


def pulse_from_evidence(analysis, notes, fallback_ms):
    """Read local pulse spacing and observed bass/texture accents, not guessed bars."""
    beats = tuple(float(t) for t in analysis.get('beats_ms', ()))
    confidence = float(analysis.get('beat_confidence', 0))
    if len(beats) < 4 or confidence < .55:
        return Pulse((), (), fallback_ms, confidence, 0.)
    attacks = [{} for _ in beats]
    for note in notes:
        index = bisect_left(beats, note.start_ms)
        choices = [i for i in (index-1, index) if 0 <= i < len(beats)]
        i = min(choices, key=lambda k: abs(beats[k]-note.start_ms))
        period = beats[min(i+1, len(beats)-1)]-beats[max(0, i-1)]
        period /= 2 if 0 < i < len(beats)-1 else 1
        if abs(beats[i]-note.start_ms) > min(100., .22*period):
            continue
        bass = note.instrument in ('electric_bass', 'acoustic_bass') or note.source == 'bass'
        weight = (2. if bass else 1.) * min(1., note.duration_ms/max(period, 1))
        # Repeated labels and octaves do not manufacture a stronger beat.
        identity = (bass, note.pitch % 12)
        attacks[i][identity] = max(weight, attacks[i].get(identity, 0.))
    values = np.array([sum(a.values()) for a in attacks])
    positive = values[values > 0]
    if len(positive) < 8:
        return Pulse(beats, (0.,)*len(beats), fallback_ms, confidence, 0.)
    low, high = np.percentile(positive, [25, 85])
    contrast = (high-low)/max(high, 1e-6)
    accent_confidence = min(1., contrast*2) * confidence
    accents = np.clip((values-low)/max(high-low, 1e-6), 0., 1.)
    return Pulse(beats, tuple(float(a) for a in accents), fallback_ms, confidence, accent_confidence)


def phrase_boundaries(lead, blocked, beat):
    boundaries = {0., *(v for pair in blocked for v in pair)}
    end, source = None, None
    for note in lead:
        current = note.extra.get('source')
        if end is not None:
            if note.time_ms-end > max(700., 1.5*beat):
                boundaries.add((note.time_ms+end)/2)
            elif current != source:
                boundaries.add(note.time_ms)
        end = max(end or 0, note.time_ms+note.duration_ms)
        source = current
    return sorted(boundaries)


def voice_path(layers, target):
    """Minimize phrase-wide motion; each layer contains only feasible pitches.

    layers: (group index, phrase id, onset, local beat, [(MIDI pitch, local cost)])
    Large evidence gaps reset the voice. Actual admission remains separate.
    """
    chosen, chain = {}, []

    def flush():
        if not chain:
            return
        scores, links = [], []
        for i, (_group, _phrase, _time, _beat, options) in enumerate(chain):
            row, back = [], []
            for pitch, local_cost in options:
                if not i:
                    row.append(local_cost+.12*abs(pitch-target))
                    back.append(-1)
                    continue
                alternatives = []
                for j, (previous, _cost) in enumerate(chain[i-1][4]):
                    distance = abs(previous-pitch)
                    motion = .16*distance + .35*max(0, distance-7) - (.25 if not distance else 0.)
                    alternatives.append(scores[-1][j]+motion)
                best = min(range(len(alternatives)), key=alternatives.__getitem__)
                row.append(local_cost+alternatives[best])
                back.append(best)
            scores.append(row)
            links.append(back)
        index = min(range(len(scores[-1])), key=scores[-1].__getitem__)
        for i in range(len(chain)-1, -1, -1):
            chosen[chain[i][0]] = chain[i][4][index][0]
            index = links[i][index]
        chain.clear()

    for layer in layers:
        if chain and (chain[-1][1] != layer[1] or layer[2]-chain[-1][2] > max(1800., 3*layer[3])):
            flush()
        if layer[4]:
            chain.append(layer)
    flush()
    return chosen
