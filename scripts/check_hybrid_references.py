"""Independent acoustic stress references; these are not real-song accuracy labels."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from autoskyplay.transcription.engine import find_engine
from autoskyplay.transcription.events import EventResult
from autoskyplay.transcription.hybrid_evidence import annotate_vocal_boundaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/hybrid-2026-10-14/references"))
    args = parser.parse_args()
    engine = find_engine(args.data, "hybrid")
    if not engine:
        raise ValueError("需要已安装的冻结混合组件")
    args.output.mkdir(parents=True, exist_ok=True)
    sr = 44100
    rows = []
    for kind in (
        "vibrato",
        "amplitude_modulation",
        "smooth_timbre",
        "reattack",
        "equal_rms_timbre",
        "short_rest",
        "cross_window",
    ):
        t = np.arange((56 if kind == "cross_window" else 16) * sr) / sr
        midi = 67 + 0.35 * np.sin(2 * np.pi * 5 * t) if kind == "vibrato" else np.full(len(t), 67.0)
        f0 = 440 * 2 ** ((midi - 69) / 12)
        phase = np.cumsum(f0) * 2 * np.pi / sr
        harmonic = np.full(len(t), 0.35)
        if kind == "smooth_timbre":
            harmonic = 0.2 + 1.5 * np.clip(t / 11, 0, 1)
        if kind == "equal_rms_timbre":
            harmonic[t >= 5] = 1.8
            harmonic[t >= 10] = 0.2
        amp = np.full(len(t), 0.15)
        if kind == "amplitude_modulation":
            amp *= 1 + 0.3 * np.sin(2 * np.pi * 5 * t)
        intervals = [(0.5, 11.0), (12.0, 12.8), (13.2, 14.0)]
        if kind in ("reattack", "equal_rms_timbre"):
            intervals = [(0.5, 5.0), (5.0, 10.0), (10.0, 11.0), (12.0, 12.8), (13.2, 14.0)]
        if kind == "short_rest":
            intervals = [(0.5, 5.0), (5.12, 11.0), (12.0, 12.8), (13.2, 14.0)]
        if kind == "cross_window":
            # All interior rests are shorter than the upstream 200 ms slicer
            # threshold, forcing the bounded inference window to be exercised.
            # The 11 s hold straddles the first 35 s decision window.
            intervals = [(0.5 + i * 3, 3.4 + i * 3) for i in range(10)]
            intervals += [(30.5, 41.5), (41.6, 44.5), (44.6, 47.5), (47.6, 50.5), (50.6, 53.5)]
        envelope = np.zeros(len(t))
        for a, b in intervals:
            part = (t >= a) & (t < b)
            envelope[part] = 1
            # Timbre-only rearticulation deliberately has no gain valley.
            if kind != "equal_rms_timbre" or a in (0.5, 12.0, 13.2):
                envelope[part] *= np.minimum(1, (t[part] - a) / 0.02)
            if kind not in ("equal_rms_timbre",) or b in (11.0, 12.8, 14.0):
                envelope[part] *= np.minimum(1, (b - t[part]) / 0.02)
        if kind == "reattack":
            for boundary in (5, 10):
                amp[(t >= boundary - 0.06) & (t < boundary)] *= 0.2
        audio = (
            amp * envelope * (np.sin(phase) + harmonic * np.sin(3 * phase)) / np.sqrt(1 + harmonic**2)
        ).astype("float32")
        folder = args.output / kind
        folder.mkdir(exist_ok=True)
        sf.write(folder / "vocals.wav", audio, sr, subtype="FLOAT")
        reference = dict(
            kind=kind,
            pitch=67,
            intervals_ms=[[a * 1000, b * 1000] for a, b in intervals],
            note="Analytic harmonic signal, outside singing training distribution; no real-song accuracy claim",
        )
        (folder / "reference.json").write_text(json.dumps(reference, indent=2))
        (folder / "request.json").write_text("{}")
        with (folder / "worker.jsonl").open("w") as out, (folder / "stderr.log").open("w") as err:
            process = subprocess.run(
                [
                    *engine.config["commands"]["game"],
                    "--work",
                    str(folder.resolve()),
                    "--models",
                    str(engine.models),
                    "--device",
                    "cpu",
                ],
                stdout=out,
                stderr=err,
            )
        if process.returncode:
            rows.append(dict(kind=kind, failed=True))
            continue
        result = EventResult.load(folder / "events.json")
        annotate_vocal_boundaries(result, folder / "vocals.wav")
        result.save(folder / "reviewed-events.json")
        expected = [a * 1000 for a, b in intervals]
        starts = [n.start_ms for n in result.raw_notes]
        matched = sum(any(abs(a - b) <= 80 for b in starts) for a in expected)
        extra = sum(not any(abs(a - b) <= 80 for b in expected) for a in starts)
        rests = [(b * 1000, c * 1000) for (_, b), (c, _) in zip(intervals, intervals[1:]) if c > b]
        filling = sum(
            max(0, min(n.start_ms + n.duration_ms, b) - max(n.start_ms, a))
            for n in result.raw_notes
            for a, b in rests
        )
        rows.append(
            dict(
                kind=kind,
                expected_events=len(intervals),
                actual_events=len(result.raw_notes),
                known_onsets_matched_80ms=matched,
                extra_onsets_80ms=extra,
                maximum_note_ms=max((n.duration_ms for n in result.raw_notes), default=0),
                known_rest_filled_ms=filling,
                boundary_review_count=sum(v["needs_review"] for v in result.analysis["boundary_reviews"]),
                pitch_within_half_semitone=sum(abs(n.raw_pitch_midi - 67) <= 0.5 for n in result.raw_notes),
                metadata=result.metadata,
                window_count=len(result.metadata.get("slices", [])),
            )
        )
        (args.output / "summary.json").write_text(json.dumps(rows, indent=2))
        print(kind, len(result.raw_notes), matched, extra, round(filling), flush=True)
    (args.output / "summary.json").write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
