"""Reproducible sustain diagnostics; rules, not real-song accuracy labels.

--prepare supplements frozen model outputs using matching separated WAV files.
Run --version before/after with the respective PYTHONPATH snapshots. It records
raw note intervals, so comparisons can distinguish joins from filled rests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np

from autoskyplay.transcription.melody import PitchResult


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--version', choices=['before', 'after'])
    args = parser.parse_args()
    manifest = json.loads((args.directory / 'manifest.json').read_text())
    evidence, rows = [], []
    for case in manifest['cases']:
        folder = args.directory / case['id']
        if args.prepare:
            from autoskyplay.transcription.boundaries import enrich_boundaries
            result = PitchResult.load(folder / 'input.npz')
            start = time.perf_counter()
            enrich_boundaries(result, Path(case.get('evidence_stems', case['new_data'])))
            elapsed = time.perf_counter() - start
            np.savez_compressed(folder / 'enriched.npz', **result.arrays())
            restored = PitchResult.load(folder / 'enriched.npz')
            original = PitchResult.load(folder / 'input.npz')
            assert all(np.array_equal(value, restored.arrays()[key]) for key, value in original.arrays().items())
            evidence.append(dict(song=case['id'], seconds=elapsed,
                                 input_sha256=hashlib.sha256((folder / 'input.npz').read_bytes()).hexdigest(),
                                 enriched_sha256=hashlib.sha256((folder / 'enriched.npz').read_bytes()).hexdigest(),
                                 original_model_features_unchanged=True,
                                 evidence_bytes=sum(v.nbytes for k, v in result.analysis.items()
                                                    if k.endswith('boundary_change'))))
        if args.version:
            from autoskyplay.transcription.segmentation import decode
            result = PitchResult.load(folder / 'enriched.npz')
            for name in ('vocals', 'other', 'bass'):
                track = result if name == 'vocals' else result.track(name)
                threshold, minimum = (.35, 80) if name == 'vocals' else (.6, 100)
                audit = []
                notes = decode(track, threshold, minimum, **({'audit': audit} if args.version == 'after' else {}))
                rows.append(dict(song=case['id'], track=name, audit=audit,
                                 notes=[[n.pitch, n.start_ms, n.duration_ms] for n in notes]))
    if args.prepare:
        (args.directory / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    if args.version:
        (args.directory / f'boundaries-{args.version}.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
