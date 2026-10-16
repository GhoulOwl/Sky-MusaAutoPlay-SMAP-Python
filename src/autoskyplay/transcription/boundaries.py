"""Small, optional acoustic checks for onset candidates; no inference runtime.

The old worker's binary onset flags lose the distinction between a new attack
and a timbre fluctuation. Inspect a short window of the SAME separated stem,
never the mixture or a different separation. Missing evidence means unknown.
"""
from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf

EVIDENCE_VERSION = 1
EVIDENCE_KEYS = ('boundary_change', 'other_boundary_change', 'bass_boundary_change')


def spectral_change(audio, rate, frequencies):
    """Largest 10 ms spectral-envelope step across overlapping 40 ms windows.

    Follow the already measured F0 and compare harmonic envelopes, so vibrato
    moving FFT peaks is not itself mistaken for a new articulation. Unexplained
    energy remains a separate component to expose consonants/noisy attacks.
    This is supporting evidence, NOT a probability of rearticulation.
    A gain attack is checked independently against the original RMS contour.
    """
    mono = np.asarray(audio, dtype=float)
    if mono.ndim == 2:
        mono = mono.mean(axis=1)
    width, hop = round(rate * .04), max(1, round(rate * .01))
    if len(mono) < width + 8 * hop or not np.isfinite(mono).all():
        return -1.
    frames = np.lib.stride_tricks.sliding_window_view(mono, width)[::hop]
    if len(frequencies) != len(frames) or min(frequencies) < 35:
        return -1.
    power = abs(np.fft.rfft(frames * np.hanning(width), axis=1)) ** 2
    bins = np.fft.rfftfreq(width, 1 / rate)
    spectrum = np.zeros((len(frames), 25))
    for i, f0 in enumerate(frequencies):
        assigned = np.zeros(len(bins), dtype=bool)
        for h in range(1, 25):
            selected = abs(bins - f0 * h) <= max(rate / width, f0 * .3)
            selected &= ~assigned
            spectrum[i, h - 1] = np.sqrt(power[i, selected].sum())
            assigned |= selected
        spectrum[i, -1] = np.sqrt(power[i, ~assigned].sum())
    norms = np.linalg.norm(spectrum, axis=1)
    if norms.min() < 1e-8:
        return -1.
    unit = spectrum / norms[:, None]
    # Gradual vowel/vibrato changes can be large over the whole window; only
    # a local step supports a new attack. RMS/pitch/confidence are checked too.
    return float(np.clip(np.max(1 - np.sum(unit[:-1] * unit[1:], axis=1)), 0, 2))


def stem_evidence(path, result, cancelled=lambda: False):
    change = np.full(len(result.frequency), -1., dtype=np.float32)
    midi = 69 + 12 * np.log2(np.maximum(result.frequency, 1) / 440)
    context = max(1, int(np.ceil(60 / result.hop_ms)))
    with sf.SoundFile(path) as stream:
        # Eighty ms either side includes the onset detector's small timing lag.
        radius = round(stream.samplerate * .08)
        for frame in np.flatnonzero(result.onset >= .5):
            if cancelled():
                raise ValueError('扒谱已取消')
            left, right = frame - context, frame + context + 1
            # Only possible sustained-tone vetoes need an FFT. Reattacks with
            # clear contour changes retain their boundary without extra work.
            if (left < 0 or right > len(midi) or np.ptp(midi[left:right]) >= .5
                    or result.confidence[left:right].min() < .8
                    or np.ptp(result.confidence[left:right]) >= .15
                    or np.ptp(result.energy_db[left:right]) >= 3):
                continue
            center = round(frame * result.hop_ms / 1000 * stream.samplerate)
            if center < radius or center + radius > len(stream):
                continue
            stream.seek(center - radius)
            audio = stream.read(2 * radius, dtype='float32', always_2d=True)
            width, step = round(stream.samplerate * .04), max(1, round(stream.samplerate * .01))
            centers = center - radius + width / 2 + np.arange((len(audio) - width) // step + 1) * step
            frequencies = np.interp(centers / stream.samplerate * 1000 / result.hop_ms,
                                    np.arange(len(result.frequency)), result.frequency)
            change[frame] = spectral_change(audio, stream.samplerate, frequencies)
    return change


def enrich_boundaries(result, directory, *, cache=False, cancelled=lambda: False):
    """Enrich new jobs or old drafts; retain a small derived cache after WAV eviction.

    Cache is bound to the exact pitch file. Never overwrite the original result,
    source audio, or edited score. Corrupt/missing evidence falls back to keeping
    onset boundaries. Atomic replacement avoids partial cache files on failure.
    """
    directory = Path(directory)
    destination = directory / 'boundary-evidence.npz'
    pitch = directory / 'pitch.npz'
    fingerprint = hashlib.sha256(pitch.read_bytes()).hexdigest() if cache and pitch.exists() else ''
    if cache and destination.exists():
        try:
            if destination.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('boundary cache too large')
            with zipfile.ZipFile(destination) as archive:
                if sum(item.file_size for item in archive.infolist()) > 4 * 1024 * 1024:
                    raise ValueError('expanded boundary cache too large')
            with np.load(destination, allow_pickle=False) as data:
                if int(data['version']) == EVIDENCE_VERSION and str(data['pitch_sha256']) == fingerprint:
                    for key in EVIDENCE_KEYS:
                        if key in data:
                            value = np.asarray(data[key], dtype=float)
                            if (value.shape == result.frequency.shape and np.isfinite(value).all()
                                    and (value >= -1).all() and (value <= 2).all()):
                                result.analysis[key] = value
        except (OSError, ValueError, TypeError, KeyError, EOFError, zipfile.BadZipFile):
            pass
    added = False
    for name, key in zip(('vocals', 'other', 'bass'), EVIDENCE_KEYS):
        if key in result.analysis or not (directory / (name + '.wav')).is_file():
            continue
        track = result if name == 'vocals' else result.track(name)
        if track is None:
            continue
        try:
            value = stem_evidence(directory / (name + '.wav'), track, cancelled)
        except (OSError, sf.LibsndfileError):
            continue
        result.analysis[key] = value
        added = True
    if cache and added and fingerprint:
        temporary = destination.with_suffix('.tmp.npz')
        try:
            np.savez_compressed(temporary, version=EVIDENCE_VERSION, pitch_sha256=fingerprint,
                                **{k: result.analysis[k] for k in EVIDENCE_KEYS if k in result.analysis})
            os.replace(temporary, destination)
        except OSError:
            temporary.unlink(missing_ok=True)
    return result
