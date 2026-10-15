"""Check delivered comparison artifacts, not musical correctness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import mido
import numpy as np
import soundfile as sf

from autoskyplay.score import load_score
from autoskyplay.transcription.events import EventResult


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path,
                        default=Path("artifacts/transcription-comparison-2026-10-14/hybrid"))
    parser.add_argument("--output", type=Path,
                        default=Path("artifacts/hybrid-2026-10-14/output-audit.json"))
    args = parser.parse_args()
    data = json.loads((args.directory / "comparison.json").read_text())
    for case in data["cases"]:
        common = []
        for version in ("A", "B", "C", "D"):
            result = EventResult.load(args.directory / f"{case['id']}-{version}-events.json")
            assert result.metadata["source_sha256"] == case["sha256"]
            common.append((result.metadata["stem_sha256"], result.analysis["beats_ms"], result.bpm))
        assert all(item == common[0] for item in common), case["id"]
    expected, scores, raw = {}, [], []
    for key in data["scores"]:
        song, version, profile = key.split("-", 2)
        score = load_score(args.directory / (key + ".json"))
        lead = [n for n in score.notes if n.extra.get("role") != "accompaniment"]
        support = [n for n in score.notes if n.extra.get("role") == "accompaniment"]
        assert profile != "Violin" or not support, key
        assert all(n.duration_ms >= 80 - 1e-5 for n in support), key
        quiet = [s for s in score.root["transcription"]["sections"] if s["source"] in ("rest", "uncertain")]
        intrusion = sum(max(0, min(n.time_ms + n.duration_ms, s["end_ms"]) - max(n.time_ms, s["start_ms"]))
                        for n in score.notes for s in quiet)
        assert intrusion < 1e-3, (key, intrusion)
        same_key_conflict = sum(max(0, min(a.time_ms+a.duration_ms, b.time_ms+b.duration_ms)
                                   - max(a.time_ms, b.time_ms))
                                for a in lead for b in support if a.key == b.key)
        assert same_key_conflict < 1e-3, (key, same_key_conflict)
        support.sort(key=lambda n: n.time_ms)
        tail_overlap = sum(max(0, a.time_ms + a.extra.get("audibleDuration", a.duration_ms) - b.time_ms)
                           for a, b in zip(support, support[1:]))
        assert tail_overlap < 1e-3, (key, tail_overlap)
        scores.append(dict(key=key, notes=len(score.notes), accompaniment=len(support),
                           uncertain_or_rest_intrusion_ms=intrusion, same_key_conflict_ms=same_key_conflict,
                           estimated_accompaniment_tail_overlap_ms=tail_overlap))
        for window in [w for w in data["windows"] if w["song"] == song]:
            for layer in ("full", "lead"):
                expected[f"{window['id']}-{version}-{profile}-{layer}.wav"] = window
        if profile == "Piano":
            result = EventResult.load(args.directory / f"{song}-{version}-events.json")
            midi = mido.MidiFile(args.directory / f"{song}-{version}-raw.mid")
            on = sum(m.type == "note_on" and m.velocity > 0 for t in midi.tracks for m in t)
            off = sum(m.type == "note_off" for t in midi.tracks for m in t)
            assert on == off == len(result.raw_notes), (song, version)
            raw.append(dict(song=song, version=version, raw_events=on, midi_tracks=len(midi.tracks)))
    sky_count = len(expected)
    for window in data["windows"]:
        for source in ("original", "vocals", "background"):
            expected[f"{window['id']}-{source}.wav"] = window
    actual = {p.name for p in (args.directory / "media").glob("*.wav")}
    assert actual == set(expected), dict(missing=list(set(expected)-actual), stale=list(actual-set(expected)))
    audio = []
    for name, window in expected.items():
        with sf.SoundFile(args.directory / "media" / name) as stream:
            expected_frames = round(window["end"] * stream.samplerate) - round(window["start"] * stream.samplerate)
            assert len(stream) == expected_frames, (name, len(stream), expected_frames)
            peak, clipped = 0.0, 0
            for block in stream.blocks(blocksize=65536, dtype="float32", always_2d=True):
                assert np.isfinite(block).all(), name
                peak = max(peak, float(np.abs(block).max(initial=0)))
                clipped += int(np.sum(np.abs(block) >= 0.999))
            assert clipped == 0, (name, clipped)
            audio.append(dict(file=name, frames=len(stream), rate=stream.samplerate, peak=peak, clipped=clipped))
    assert len(data["render_audit"]) == sky_count
    output = dict(sky_clips=sky_count, reference_clips=len(expected)-sky_count, score_count=len(scores),
                  raw_midi_count=len(raw), maximum_audio_peak=max(a["peak"] for a in audio),
                  scores=scores, raw=raw, audio=audio, excluded_failures=data["failures"],
                  scope="Artifact consistency and engineering constraints only; no real-song accuracy labels")
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print({k: v for k, v in output.items() if k not in ("scores", "raw", "audio")})


if __name__ == "__main__":
    main()
