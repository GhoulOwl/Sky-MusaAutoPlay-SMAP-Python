"""Advisory checks on native events; never rewrite a GAME onset or offset."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import soundfile as sf

from .boundaries import spectral_change


def annotate_vocal_boundaries(result, path, cancelled=lambda: False):
    notes = sorted((n for n in result.raw_notes if n.audio_source == "vocals"), key=lambda n: n.start_ms)
    reviews, by_id = [], {}
    with sf.SoundFile(path) as stream:
        rate = stream.samplerate
        radius = round(0.08 * rate)
        for first, second in zip(notes, notes[1:]):
            if cancelled():
                raise ValueError("扒谱已取消")
            gap = second.start_ms - first.start_ms - first.duration_ms
            p = first.raw_pitch_midi if first.raw_pitch_midi is not None else first.pitch
            q = second.raw_pitch_midi if second.raw_pitch_midi is not None else second.pitch
            if not -1 <= gap <= 20 or abs(p - q) > 0.35:
                continue
            center = round(second.start_ms / 1000 * rate)
            if center < radius or center + radius > len(stream):
                continue
            stream.seek(center - radius)
            audio = stream.read(2 * radius, dtype="float32", always_2d=True).mean(axis=1)
            width, step = round(0.04 * rate), round(0.01 * rate)
            count = (len(audio) - width) // step + 1
            f0 = 440 * 2 ** (((p + q) / 2 - 69) / 12)
            change = spectral_change(audio, rate, np.full(count, f0))
            rms = np.array(
                [np.sqrt(np.mean(audio[i : i + step] ** 2)) for i in range(0, len(audio) - step + 1, step)]
            )
            spread = float(20 * np.log10(max(1e-10, rms.max()) / max(1e-10, rms.min())))
            if change < 0:
                continue
            suspicious = change < 0.01 and spread < 3
            review = dict(
                start_ms=second.start_ms,
                end_ms=min(result.duration_ms, second.start_ms + 80),
                event_ids=[first.identity, second.identity],
                spectral_step=change,
                rms_spread_db=spread,
                reason="相邻同音边界缺少明显新起音证据，待试听；保留两个原始事件"
                if suspicious
                else "同音边界有音色或响度变化支持，保留独立起音",
                needs_review=suspicious,
                action="preserve",
            )
            reviews.append(review)
            if suspicious:
                by_id[second.identity] = review["reason"]
    result.raw_notes = [
        replace(n, review_reasons=(*n.review_reasons, by_id[n.identity])) if n.identity in by_id else n
        for n in result.raw_notes
    ]
    result.analysis["boundary_reviews"] = reviews
    result.metadata["boundary_evidence_version"] = "native-advisory-1"
    return result
