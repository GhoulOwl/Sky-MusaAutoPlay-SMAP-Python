"""Paired P1/P2 diagnostics on immutable real-song features (not accuracy scores).

Run with PYTHONPATH pointing to either saved baseline code or current src.
Audio/model inference is deliberately excluded from these paired comparisons.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import time
from collections import Counter
from pathlib import Path

import numpy as np
import soundfile as sf

from autoskyplay.midi import SKY_MIDI
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.melody import PitchResult


def tail_durations(assets, instrument):
    durations = []
    for key in range(15):
        audio, rate = sf.read(assets / instrument / f'{key}.wav', always_2d=True)
        hop = max(1, round(rate * .02))
        rms = np.array([np.sqrt(np.mean(audio[i:i + hop] ** 2)) for i in range(0, len(audio), hop)])
        audible = np.flatnonzero(rms > rms.max() * .01)
        durations.append(min(len(audio), (audible[-1] + 1) * hop) / rate * 1000 if len(audible) else 0)
    return durations


def overlap_metrics(notes, tails, instrument):
    events = []
    for note in notes:
        duration = max(note.duration_ms, tails[note.key]) if instrument == 'Piano' else note.duration_ms + 1500 / 44.1
        events.extend([(note.time_ms, 1), (note.time_ms + duration, -1)])
    active, maximum, overlap, last = 0, 0, 0., 0.
    for t, step in sorted(events):
        if active >= 2:
            overlap += t - last
        active += step
        maximum = max(maximum, active)
        last = t
    return dict(max_accompaniment_voices=maximum, accompaniment_overlap_ms=overlap)


def diagnostics(score, result, instrument, tails):
    lead = sorted((n for n in score.notes if n.extra['role'] == 'melody'), key=lambda n: n.time_ms)
    accompaniment = [n for n in score.notes if n.extra['role'] == 'accompaniment']
    reversals, collapsed, octave_jumps, pairs = [], [], [], 0
    for a, b in zip(lead, lead[1:]):
        if b.time_ms - a.time_ms - a.duration_ms > 600 or a.extra['source'] != b.extra['source']:
            continue
        raw = b.extra['sourcePitch'] - a.extra['sourcePitch']
        mapped = SKY_MIDI[b.key] - SKY_MIDI[a.key]
        pairs += 1
        entry = dict(start_ms=a.time_ms, end_ms=b.time_ms, source_interval=raw, mapped_interval=mapped)
        if raw * mapped < 0:
            reversals.append(entry)
        if raw and not mapped:
            collapsed.append(entry)
        if abs(raw) <= 5 and abs(mapped) >= 10:
            octave_jumps.append(entry)
    meta = score.root['transcription']
    return dict(melody_notes=len(lead), accompaniment_notes=len(accompaniment),
                source_counts=dict(Counter(n.extra['source'] for n in lead)),
                melody_identity=[[n.extra[k] for k in ('source', 'sourceTime', 'sourcePitch', 'sourceDuration')]
                                 + [n.time_ms, n.duration_ms] for n in lead],
                mapped_contour_reversals=reversals, collapsed_intervals=collapsed,
                added_octave_jumps=octave_jumps, adjacent_pairs=pairs,
                tiny_accompaniment=sum(n.duration_ms < 80 - 1e-6 for n in accompaniment),
                uncertain_ms=sum(s['end_ms'] - s['start_ms'] for s in meta['sections'] if s['needs_review']),
                sections=meta['sections'], **overlap_metrics(accompaniment, tails, instrument))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('version', choices=['before', 'after'])
    parser.add_argument('--input-name', default='input.npz', choices=['input.npz', 'enriched.npz'])
    args = parser.parse_args()
    manifest = json.loads((args.directory / 'manifest.json').read_text())
    assets = Path(__file__).resolve().parents[1] / 'src/autoskyplay/assets/instruments'
    supports_instrument = 'instrument' in inspect.signature(build_score).parameters
    rows = []
    for case in manifest['cases']:
        folder = args.directory / case['id']
        result = PitchResult.load(folder / args.input_name)
        for instrument, mode in [('Piano', 'auto'), ('Violin', 'auto'), ('Violin', 'simple')]:
            variant = instrument + ('-accompaniment' if mode == 'simple' else '')
            options = dict(instrument=instrument, mode=mode) if supports_instrument else dict(mode='simple')
            start = time.perf_counter()
            score, stats = build_score(result, case['title'], **options)
            elapsed = time.perf_counter() - start
            score.save(folder / f'{args.version}-{variant}-score.json')
            row = dict(song=case['id'], title=case['title'], instrument=instrument, variant=variant,
                       seconds=elapsed, duration_ms=result.duration_ms, stats=stats,
                       input_sha256=hashlib.sha256((folder / args.input_name).read_bytes()).hexdigest(),
                       **diagnostics(score, result, instrument, tail_durations(assets, instrument)))
            rows.append(row)
            print(case['title'], variant, 'melody', row['melody_notes'], 'accomp', row['accompaniment_notes'],
                  'reversals', len(row['mapped_contour_reversals']), 'tail overlap',
                  round(row['accompaniment_overlap_ms']), 'ms', round(elapsed, 3), 's', flush=True)
    (args.directory / f'{args.version}.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
