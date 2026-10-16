"""Offline signal features shared by source and frozen workers; no app imports."""
from __future__ import annotations

import numpy as np


def pitch_track(audio, sr, emit, name, low=60, high=1500):
    import librosa
    import torch
    import torchcrepe

    mono = librosa.resample(audio.mean(axis=1), orig_sr=sr, target_sr=16000)
    frequencies, confidences = [], []
    hop, block, margin = 160, 16000 * 20, 16000
    for offset in range(0, len(mono), block):
        left, right = max(0, offset - margin), min(len(mono), offset + block + margin)
        with torch.inference_mode():
            pitch, confidence = torchcrepe.predict(
                torch.from_numpy(mono[left:right].copy())[None], 16000, hop_length=hop,
                fmin=low, fmax=high, model='full', batch_size=32, device='cpu', return_periodicity=True)
        begin = (offset - left) // hop
        count = min(block, len(mono) - offset)
        frames = (count + hop - 1) // hop
        frequencies.append(pitch[0, begin:begin + frames].numpy())
        confidences.append(confidence[0, begin:begin + frames].numpy())
        emit('pitch', min(100, int(100 * (offset + count) / len(mono))), source=name)
    frequency = np.nan_to_num(np.concatenate(frequencies), nan=0, posinf=0, neginf=0)
    confidence = np.clip(np.nan_to_num(np.concatenate(confidences)), 0, 1)
    count = len(frequency)
    energy = librosa.feature.rms(y=mono, frame_length=1024, hop_length=hop)[0]
    energy = np.pad(energy, (0, max(0, count - len(energy))), mode='edge')[:count]
    energy_db = 20 * np.log10(np.maximum(energy, 1e-8))
    confidence[energy_db < -65] = 0
    onset = np.zeros(count, dtype='float32')
    attacks = librosa.onset.onset_detect(y=mono, sr=16000, hop_length=hop, backtrack=False)
    onset[attacks[attacks < count]] = 1
    return dict(frequency=frequency, confidence=confidence, energy_db=energy_db, onset=onset)


def rhythm_harmony(original, drums, harmonic, sr, duration):
    import librosa

    empty = dict(beats_ms=np.array([]), beat_confidence=np.array(0.),
                 chroma=np.empty((0, 12)), harmony_energy_db=np.array([]))
    if duration < 1.5 or np.max(abs(original)) < 1e-6:
        return empty, 120., False
    rate, hop = 22050, 512
    mix = librosa.resample(original.mean(axis=1), orig_sr=sr, target_sr=rate)
    pulse = librosa.resample(drums.mean(axis=1), orig_sr=sr, target_sr=rate)
    # Acoustic songs may have no drums. Use mixture for pulse estimation then.
    if np.mean(pulse ** 2) < np.mean(mix ** 2) * .015:
        pulse = mix
    envelope = librosa.onset.onset_strength(y=pulse, sr=rate, hop_length=hop)
    tempo, frames = librosa.beat.beat_track(onset_envelope=envelope, sr=rate, hop_length=hop)
    bpm = float(np.asarray(tempo).reshape(-1)[0])
    valid = len(frames) >= 4 and 30 <= bpm <= 300
    beats = librosa.frames_to_time(frames, sr=rate, hop_length=hop) * 1000 if valid else np.array([])
    beats = beats[beats < duration * 1000]
    if len(beats) < 4:
        return empty, 120., False
    confidence = 0.
    if valid and len(beats) >= 4:
        intervals = np.diff(beats)
        regularity = np.clip(1 - np.std(intervals) / max(1, np.mean(intervals)) * 3, 0, 1)
        support = np.mean(envelope[frames]) / max(float(np.percentile(envelope, 90)), 1e-8)
        confidence = float(regularity * np.clip(support, 0, 1))
    harmony = librosa.resample(harmonic.mean(axis=1), orig_sr=sr, target_sr=rate)
    chroma = librosa.feature.chroma_cqt(y=harmony, sr=rate, hop_length=hop, tuning=0)
    energy = librosa.feature.rms(y=harmony, hop_length=hop)[0]
    rows, levels = [], []
    for a, b in zip(beats[:-1], beats[1:]):
        left = int(a / 1000 * rate / hop)
        right = max(left + 1, int(b / 1000 * rate / hop))
        rows.append(np.mean(chroma[:, left:right], axis=1))
        levels.append(20 * np.log10(max(1e-8, float(np.mean(energy[left:right])))))
    return dict(beats_ms=beats, beat_confidence=np.array(confidence),
                chroma=np.asarray(rows).reshape(-1, 12), harmony_energy_db=np.asarray(levels)), bpm if valid else 120., valid
