"""One offline rhythm/key reference; estimates never rewrite pitches or timing."""

from __future__ import annotations

import copy
from pathlib import Path

import numpy as np

from .multistem_models import verify_asset
from .signal_analysis import harmony_at_beats, rhythm_harmony

PITCH_NAMES = ('C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B')
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def validate_beats(beats, duration_ms):
    times = np.asarray(beats, dtype=float)
    if (times.ndim != 1 or len(times) > 10000 or not np.isfinite(times).all()
            or (times < 0).any() or (times > duration_ms).any() or (np.diff(times) <= 1).any()):
        raise ValueError('拍点须按时间递增、位于选段内，相邻间隔大于 1 ms，最多 10000 个')
    return times


def refine_beats(audio, sr, beats):
    """Bounded local transient proposals; never fill gaps or extrapolate edges."""
    import librosa

    if len(beats) < 4 or not np.any(audio):
        return np.asarray(beats), 0
    rate, hop = 22050, 64
    y = librosa.resample(audio.mean(axis=1), orig_sr=sr, target_sr=rate)
    envelope = librosa.onset.onset_strength(y=y, sr=rate, hop_length=hop)
    floor = max(1e-8, float(np.percentile(envelope, 75)))
    result, refined = list(beats), 0
    for i, beat in enumerate(beats):
        frame = round(beat / 1000 * rate / hop)
        radius = round(.025 * rate / hop)
        lo, hi = max(0, frame - radius), min(len(envelope), frame + radius + 1)
        if hi <= lo:
            continue
        peak = lo + int(np.argmax(envelope[lo:hi]))
        time = peak * hop / rate * 1000
        if envelope[peak] > floor and abs(time - beat) <= 20:
            result[i], refined = time, refined + 1
    out = np.asarray(result)
    if refined < 2 or (np.diff(out) <= 1).any():
        return np.asarray(beats), 0
    return out, refined


def detect_rhythm(original, drums, harmonic, sr, models, *, detector='auto'):
    if detector not in ('auto', 'librosa', 'model'):
        raise ValueError('节奏检测器无效')
    duration_ms = len(original) / sr * 1000
    analysis, bpm, valid = rhythm_harmony(original, drums, harmonic, sr, duration_ms / 1000)
    fallback_bpm, fallback_valid, reason = bpm, valid, ''
    source, raw, downbeats = 'librosa', analysis['beats_ms'], np.array([])
    if detector != 'librosa' and duration_ms >= 1500 and np.max(abs(original)) >= 1e-6:
        try:
            # Verify existence before calling Audio2Beats: a missing path would
            # otherwise be interpreted as a downloadable checkpoint name.
            path = verify_asset(Path(models), 'beat')
            from beat_this.inference import Audio2Beats
            # A held file stream cannot fall through the library's missing-path
            # handler into torch.hub network downloads if files are evicted.
            with path.open('rb') as checkpoint:
                predictor = Audio2Beats(checkpoint_path=checkpoint, device='cpu', dbn=False)
            raw_seconds, down_seconds = predictor(original.mean(axis=1), sr)
            raw = validate_beats(np.asarray(raw_seconds) * 1000, duration_ms)
            downbeats = validate_beats(np.asarray(down_seconds) * 1000, duration_ms)
            if len(raw) < 4:
                raise ValueError('神经节奏检测返回的拍点不足')
            bpm = float(60000 / np.median(np.diff(raw)))
            if not 30 <= bpm <= 300:
                raise ValueError('神经节奏检测速度超出支持范围')
            valid, source = True, 'beat_this'
            # Agreement is an internal check, never a calibrated accuracy score.
            intervals = np.diff(raw)
            analysis['beat_confidence'] = np.array(float(np.clip(1 - np.std(intervals) / np.mean(intervals) * 3, 0, .9)))
        except Exception as exc:
            if detector == 'model':
                raise ValueError('指定的神经节奏检测不可用：' + str(exc)[:200]) from exc
            reason = str(exc)[:300]
            raw = analysis['beats_ms']
            downbeats = np.array([])
            bpm, valid = fallback_bpm, len(raw) >= 4
    refined, count = refine_beats(drums, sr, raw)
    # Restrict downbeats to observations close to actual detected beats.
    downbeats = np.array([b for b in downbeats if len(raw) and np.min(abs(raw - b)) <= 80])
    candidates = sorted({round(bpm / 2, 3), round(bpm, 3), round(bpm * 2, 3)}) if valid else []
    conflict = source == 'beat_this' and fallback_valid and fallback_bpm > 0 and (
        abs(bpm / fallback_bpm - 2) < .12 or abs(fallback_bpm / bpm - 2) < .12)
    if conflict:
        analysis['beat_confidence'] = np.array(min(.5, float(analysis['beat_confidence'])))
    analysis.update(harmony_at_beats(harmonic, sr, refined), beats_ms=refined,
                    detected_beats_ms=np.asarray(raw), downbeats_ms=downbeats)
    receipt = dict(detector=source, neural_source='original', fallback_source='drums_or_mix',
                   fallback_reason=reason, fallback_bpm=fallback_bpm,
                   bpm_candidates=[v for v in candidates if 30 <= v <= 300],
                   half_double_conflict=bool(conflict), needs_review=bool(conflict),
                   refined_count=count, extrapolated_beats=0)
    return analysis, bpm, valid, receipt


def detect_key(harmony, bass, sr, *, starts_song=False, ends_song=False):
    """Rank key hypotheses, retaining relative-key ambiguity and source scope."""
    import librosa

    if not len(harmony) or float(np.max(abs(harmony))) < 1e-8:
        return dict(candidates=[], ambiguous=True, reason='没有足够的有音高音频')
    rate, hop = 22050, 2048
    y = librosa.resample(harmony.mean(axis=1), orig_sr=sr, target_sr=rate)
    low = librosa.resample(bass.mean(axis=1), orig_sr=sr, target_sr=rate)
    chroma = librosa.feature.chroma_cqt(y=y, sr=rate, hop_length=hop)
    low_chroma = librosa.feature.chroma_cqt(y=low, sr=rate, hop_length=hop,
                                           fmin=librosa.note_to_hz('C1'), n_octaves=3)
    level = librosa.feature.rms(y=y, hop_length=hop)[0]
    count = min(chroma.shape[1], low_chroma.shape[1], len(level))
    chroma, low_chroma, level = chroma[:, :count], low_chroma[:, :count], level[:count]
    active = np.flatnonzero(level > max(1e-8, float(level.max()) * .1))
    if not len(active):
        return dict(candidates=[], ambiguous=True, reason='有音高音频低于有效门限')
    histogram = chroma[:, active].mean(axis=1)
    edges, span = [], max(1, round(4 * rate / hop))
    if starts_song:
        edges.append(slice(active[0], min(count, active[0] + span)))
    if ends_song:
        edges.append(slice(max(0, active[-1] - span + 1), active[-1] + 1))
    ranked = []
    for mode, profile in (('major', MAJOR), ('minor', MINOR)):
        for root in range(12):
            corr = float(np.corrcoef(histogram, np.roll(profile, root))[0, 1]) if np.std(histogram) > 1e-8 else 0.
            tonic = 0.
            triad = [root, (root + (4 if mode == 'major' else 3)) % 12, (root + 7) % 12]
            for edge in edges:
                h, b = chroma[:, edge].mean(axis=1), low_chroma[:, edge].mean(axis=1)
                tonic += float(np.mean(h[triad]) / max(1e-8, h.max()) + b[root] / max(1e-8, b.max()))
            score = corr + (.15 * tonic / len(edges) if edges else 0.)
            ranked.append(dict(tonic=root, mode=mode, label=f'{PITCH_NAMES[root]} {mode}',
                               correlation=round(corr, 4), score=round(score, 4)))
    ranked.sort(key=lambda c: c['score'], reverse=True)
    return dict(candidates=ranked[:3], ambiguous=ranked[0]['score'] - ranked[1]['score'] < .08,
                method='chroma-profile-bass-edges-1', starts_song=starts_song, ends_song=ends_song,
                scope='whole_song' if starts_song and ends_song else 'selection',
                confidence_kind='uncalibrated ranking; not accuracy', applied_to_transposition=False)


def edit_rhythm(result, beats):
    """Edit the analysis only, preserving original events and acoustic evidence."""
    times = validate_beats(beats, result.duration_ms)
    if len(times) < 4:
        raise ValueError('至少保留四个拍点')
    bpm = float(60000 / np.median(np.diff(times)))
    if not 30 <= bpm <= 300:
        raise ValueError('拍点对应速度须在 30–300 BPM')
    changed = copy.deepcopy(result)
    a = changed.analysis
    a.setdefault('rhythm_original', {k: copy.deepcopy(a[k]) for k in
                 ('beats_ms', 'beat_confidence', 'chroma', 'harmony_energy_db', 'downbeats_ms') if k in a})
    old = a['rhythm_original']
    old_beats = np.asarray(old.get('beats_ms', []))
    old_chroma = np.asarray(old.get('chroma', []))
    old_energy = np.asarray(old.get('harmony_energy_db', []))
    rows, levels = [], []
    for start, end in zip(times[:-1], times[1:]):
        overlap = np.maximum(0, np.minimum(old_beats[1:], end) - np.maximum(old_beats[:-1], start))
        if (len(overlap) and len(old_chroma) == len(overlap) and len(old_energy) == len(overlap)
                and overlap.sum() >= .8 * (end - start)):
            rows.append(np.average(old_chroma, axis=0, weights=overlap).tolist())
            power = np.average(10 ** (old_energy / 10), weights=overlap)
            levels.append(float(10 * np.log10(max(1e-16, power))))
        else:
            rows.append([0.] * 12)
            levels.append(-160.)
    a.update(beats_ms=times.tolist(), beat_confidence=1., chroma=rows, harmony_energy_db=levels,
             downbeats_ms=[], rhythm_user_edited=True)
    changed.bpm = bpm
    changed.metadata.update(bpm_estimated=True, rhythm_reference='user-edited')
    changed.validate()
    return changed
