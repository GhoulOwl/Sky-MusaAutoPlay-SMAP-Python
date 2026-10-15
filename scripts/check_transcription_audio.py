"""Known audio references for beat/chord analysis. Run with the engine Python.

These are generated from bundled samples/sinusoids, not real-song accuracy.
The JSON result can be consumed with the desktop environment's chord decoder.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/autoskyplay/transcription'))
from signal_analysis import rhythm_harmony  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rate = 44100
    cases = []
    # Independent major and minor references with different timbres/inversions.
    for name, pitches in (('C-major', (60, 64, 67)), ('A-minor', (57, 60, 64)),
                          ('F-major-inversion', (57, 60, 65)), ('G-major', (55, 59, 62))):
        audio = np.zeros((rate * 8, 2), np.float32)
        for beat in range(16):
            t = np.arange(rate) / rate
            tone = sum(sum(np.sin(2 * np.pi * (440 * 2 ** ((p - 69) / 12)) * h * t) / h
                           for h in range(1, 5)) for p in pitches)
            tone *= np.minimum(1, t / .01) * np.exp(-5 * t) * .035
            start = int(beat * .5 * rate)
            count = min(len(tone), len(audio) - start)
            audio[start:start + count] += tone[:count, None]
        features, bpm, _ = rhythm_harmony(audio, np.zeros_like(audio), audio, rate, 8)
        cases.append(dict(name=name, expected_pcs=sorted({p % 12 for p in pitches}),
                          bpm=bpm, confidence=float(features['beat_confidence']),
                          chroma=features['chroma'].tolist()))
        sf.write(args.output / (name + '.wav'), audio, rate)
    # Bundled piano has unequal note loudness and realistic overtones/decay.
    audio = np.zeros((rate * 8, 2), np.float32)
    for beat in range(16):
        for key in (0, 2, 4):
            sample, sr = sf.read(ROOT / f'src/autoskyplay/assets/instruments/Piano/{key}.wav',
                                 dtype='float32', always_2d=True)
            assert sr == rate
            if sample.shape[1] == 1:
                sample = np.repeat(sample, 2, axis=1)
            start = int(beat * .5 * rate)
            count = min(len(sample), len(audio) - start)
            audio[start:start + count] += sample[:count] * .1
    features, bpm, _ = rhythm_harmony(audio, np.zeros_like(audio), audio, rate, 8)
    cases.append(dict(name='sampled-piano-C', expected_pcs=[0, 4, 7], bpm=bpm,
                      confidence=float(features['beat_confidence']), chroma=features['chroma'].tolist()))
    sf.write(args.output / 'sampled-piano-C.wav', audio, rate)
    (args.output / 'features.json').write_text(json.dumps(cases, indent=2) + '\n')
    print('Audio references:', len(cases), '→', args.output)


if __name__ == '__main__':
    main()
