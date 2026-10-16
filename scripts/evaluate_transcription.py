"""Reproducible contour benchmark. Ground truth is independent of the decoder.

Run before/after changes with --output. This evaluates segmentation, not source
separation or real-song accuracy. --reference/--estimate also compares manually
annotated note JSON arrays [pitch, start_ms, duration_ms] using bipartite matching.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from autoskyplay.transcription.melody import PitchResult

ROOT = Path(__file__).resolve().parents[1]


def fixture(case):
    count = int(case['duration'] / 10)
    pitch = np.full(count, 60.)
    confidence = np.full(count, case.get('silent_confidence', 0.))
    energy = np.full(count, -100.)
    onset = np.zeros(count)
    for note, start, duration in case['notes']:
        a, b = int(start / 10), int((start + duration) / 10)
        pitch[a:b] = note + case.get('vibrato', 0) * np.sin(np.arange(b - a) * 2 * np.pi * 5 / 100)
        confidence[a:b] = .9
        energy[a:b] = -20 if start < case.get('quiet_after', 1e10) else -55
        onset[a] = 1
    for start, end in case.get('dropouts', []):
        confidence[int(start / 10):int(end / 10)] = .1
    return PitchResult(440 * 2 ** ((pitch - 69) / 12), confidence, energy, onset,
                       duration_ms=case['duration'])


def metrics(reference, estimate, offsets=True):
    # Maximum one-to-one matching, with onset 50 ms, pitch 50 cents,
    # and offset max(50 ms, 20% of reference duration) tolerances.
    edges = [[j for j, (p, s, d) in enumerate(estimate)
              if abs(p - rp) <= .5 and abs(s - rs) <= 50
              and (not offsets or abs(s + d - rs - rd) <= max(50, .2 * rd))]
             for rp, rs, rd in reference]
    matches = {}
    def augment(i, seen):
        for j in edges[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in matches or augment(matches[j], seen):
                matches[j] = i
                return True
        return False
    for i in range(len(reference)):
        augment(i, set())
    tp = len(matches)
    precision = tp / len(estimate) if estimate else float(not reference)
    recall = tp / len(reference) if reference else float(not estimate)
    return dict(precision=precision, recall=recall,
                f1=2 * precision * recall / (precision + recall) if precision + recall else 0,
                extra=len(estimate) - tp, missing=len(reference) - tp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--estimate', type=Path)
    args = parser.parse_args()
    if args.reference and args.estimate:
        report = metrics(json.loads(args.reference.read_text()), json.loads(args.estimate.read_text()))
    else:
        cases = json.loads((ROOT / 'tests/fixtures/transcription/contours.json').read_text())['cases']
        rows = []
        for case in cases:
            notes = fixture(case).notes()
            predicted = [[n.pitch, n.start_ms, n.duration_ms] for n in notes]
            rows.append(dict(name=case['name'], split=case['split'], predicted=predicted,
                             onset=metrics(case['notes'], predicted, False),
                             offset=metrics(case['notes'], predicted)))
        report = dict(scope='synthetic contours only; not real-song accuracy', cases=rows,
                      macro_f1=float(np.mean([r['offset']['f1'] for r in rows])))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'cases'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
