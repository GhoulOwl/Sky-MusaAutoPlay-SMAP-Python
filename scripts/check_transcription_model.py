"""Known-pitch reference check, run with .transcription-venv's Python."""

from __future__ import annotations

import json

import numpy as np
import torch
import torchcrepe


def main():
    sample_rate = 16000
    reference = [60, 62, 64, 67, 69, 72]
    chunks = []
    for pitch in reference:
        time = np.arange(int(sample_rate * .6)) / sample_rate
        frequency = 440 * 2 ** ((pitch - 69) / 12)
        envelope = np.minimum(1, time / .03) * np.minimum(1, (.6 - time) / .03)
        waveform = sum(np.sin(2 * np.pi * frequency * k * time) / k for k in range(1, 5)) * .15 * envelope
        chunks.extend([waveform, np.zeros(int(sample_rate * .12))])
    audio = np.concatenate(chunks).astype("float32")
    torch.set_num_threads(2)
    np.random.seed(0)
    with torch.inference_mode():
        pitch, _ = torchcrepe.predict(torch.from_numpy(audio)[None], sample_rate, hop_length=160,
                                      model="full", batch_size=32, return_periodicity=True)
    results = []
    for i, expected in enumerate(reference):
        start, end = int((i * .72 + .1) * 100), int((i * .72 + .5) * 100)
        midi = 69 + 12 * np.log2(pitch[0, start:end].numpy() / 440)
        results.append(dict(expected=expected, median_pitch=round(float(np.median(midi)), 3),
                            frames_within_half_semitone=float(np.mean(np.abs(midi - expected) < .5))))
    print(json.dumps(results, indent=2))
    if not all(r["frames_within_half_semitone"] >= .95 for r in results):
        raise SystemExit("Known-pitch reference failed")


if __name__ == "__main__":
    main()
