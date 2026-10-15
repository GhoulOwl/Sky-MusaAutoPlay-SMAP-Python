"""Four branch ablations, an original-mix comparator and a no-drums control."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import numpy as np
import soundfile as sf
from evaluate_muscriptor import metrics, render

from autoskyplay.audio import SAMPLE_RATE
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.boundaries import enrich_boundaries
from autoskyplay.transcription.events import EventResult, ModelNote
from autoskyplay.transcription.melody import PitchResult
from autoskyplay.transcription.models import digest

VERSIONS = {
    "A": "A · 旧人声＋旧背景",
    "B": "B · GAME＋旧背景",
    "C": "C · 旧人声＋MuScriptor背景",
    "D": "D · GAME＋MuScriptor背景",
    "E": "E · MuScriptor原混音",
    "D0": "D0 · 混合去鼓背景",
}
FINDINGS = {
    "island-long": "重点核对约 208.5 秒：旧人声和原混音转到 MIDI 65，GAME 仍保持 MIDI 63。长音变长可能吞掉真实变音，不能直接判为改善。",
    "island-outro": "D / E 最长事件约 2.78 / 8.88 秒，音尾差异很大。请对照背景确认主题与实际结束位置。",
    "island-interlude": "D / E 各有 18 个旋律事件，最长约 3.15 / 3.20 秒；当前没有明显结构收益，仍需核对器乐主题。",
    "zugou-sustain": "D 有 9 个旋律事件，E 仅 1 个。人声候选增加，但是否恢复了正确旋律仍须对照分离人声。",
    "zugou-long": "D / E 最长旋律约 0.91 / 0.76 秒；此处不能视为长音问题已完全解决。",
    "zugou-contour": "约 42.45、54.15 秒仍有缺少明显新起音证据的同音边界；保留候选并标记，未自动合并。",
    "island-accomp": "约 43.87、55.91 秒的同音边界仍待复核。切换只听主旋律可避免伴奏掩盖起音差异。",
    "love-long": "GAME 原始候选约 33.18 秒开始、持续 1.28 秒；最终时值与音高均需对照人声，不能只看持续长度。",
}


def legacy_events(pitch):
    notes = []
    for name, threshold, minimum, instrument in (
        ("vocals", 0.35, 80, "voice"),
        ("other", 0.6, 100, "legacy_other"),
        ("bass", 0.6, 100, "acoustic_bass"),
    ):
        track = pitch if name == "vocals" else pitch.track(name)
        for n in track.notes(threshold, minimum):
            notes.append(
                ModelNote(
                    n.pitch,
                    n.start_ms,
                    n.duration_ms,
                    instrument,
                    len(notes),
                    provider="legacy",
                    audio_source="vocals" if name == "vocals" else "background",
                    event_id=f"legacy:{name}:{len(notes)}",
                    model_confidence=n.confidence,
                )
            )
    return notes


def combine(base, notes, version, legacy_metadata):
    has_game = any(n.provider == "game" for n in notes)
    has_background = any(n.provider == "muscriptor" for n in notes)
    identities = {n.identity for n in notes}
    analysis = dict(base.analysis)
    analysis["boundary_reviews"] = [
        r for r in analysis.get("boundary_reviews", []) if set(r["event_ids"]) <= identities
    ]
    if not has_game:
        analysis["game_regions"] = []
        analysis["game_window_review"] = []
    metadata = {
        k: base.metadata[k]
        for k in (
            "source_sha256",
            "selection",
            "stem_sha256",
            "stem_pcm_sha256",
            "duration_ms",
            "bpm_estimated",
        )
    }
    metadata.update(
        ablation=version,
        backend="hybrid",
        engine="hybrid-ablation-1",
        branches=dict(
            vocals=base.metadata["branches"]["vocals"] if has_game else legacy_metadata,
            background=base.metadata["branches"]["background"] if has_background else legacy_metadata,
        ),
    )
    return EventResult(
        [replace(n, index=i) for i, n in enumerate(notes)],
        base.duration_ms,
        base.bpm,
        metadata,
        analysis,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=Path("artifacts/hybrid-2026-10-14"))
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/transcription-comparison-2026-10-14/hybrid")
    )
    parser.add_argument("--recognize-legacy", action="store_true")
    parser.add_argument("--skip-audio", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    cases = json.loads((args.raw / "manifest.json").read_text())["cases"]
    cases = [{k: c[k] for k in ("id", "title", "artist", "length", "start", "sha256", "evaluated_range")
              if k in c} for c in cases]
    windows = json.loads(
        (root / "artifacts/transcription-comparison-2026-10-14/sustain/windows.json").read_text()
    )
    for window in windows:
        window.pop("finding", None)
        window["label"] = window["label"].replace("延音切分修复", "延音切分重点")
        window["finding"] = FINDINGS.get(window["id"], "对照原曲与分轨，检查主题、真实重奏和休止；数量变化不代表准确率变化。")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "media").mkdir(exist_ok=True)
    data = dict(
        cases=cases, windows=windows, versions=VERSIONS, scores={}, metrics=[], render_audit=[], provenance=[],
        failures=[],
    )
    for case in cases:
        folder = args.raw / f"full-{case['id']}"
        completed = json.loads((folder / "complete.json").read_text())
        if completed["events_sha256"] != digest(folder / "events.json"):
            raise ValueError("对照输入不属于当前完整成功任务")
        base = EventResult.load(folder / "events.json")
        if digest(folder / "source.audio") != case["sha256"]:
            raise ValueError("源音频与冻结基线不同")
        if args.recognize_legacy and not (folder / "legacy-metadata.json").exists():
            with (folder / "legacy.jsonl").open("w") as out, (folder / "legacy.stderr").open("w") as err:
                subprocess.run(
                    [
                        root / ".transcription-venv/bin/python",
                        root / "src/autoskyplay/transcription/worker.py",
                        "--work",
                        folder.resolve(),
                        "--models",
                        root / "build/transcription-models",
                        "--pitch-only",
                    ],
                    stdout=out,
                    stderr=err,
                    check=True,
                )
            done = [
                json.loads(s) for s in (folder / "legacy.jsonl").read_text().splitlines() if s.startswith("{")
            ]
            meta = next(v for v in done if v.get("stage") == "done")
            (folder / "legacy-metadata.json").write_text(json.dumps(meta))
        pitch = PitchResult.load(
            folder / "pitch.npz", json.loads((folder / "legacy-metadata.json").read_text())
        )
        enrich_boundaries(pitch, folder, cache=True)
        old = legacy_events(pitch)
        game = [n for n in base.raw_notes if n.audio_source == "vocals"]
        background = [n for n in base.raw_notes if n.audio_source != "vocals"]
        # E is an explicitly frozen original-mix recognition, not a falsely
        # claimed same-run benchmark. Its input hash is verified separately.
        epath = root / f"artifacts/muscriptor-2026-10-14/small-{case['id']}"
        if digest(epath / "source.audio") != case["sha256"]:
            raise ValueError("E 组原混音源哈希不一致")
        original = EventResult.load(epath / "events.json")
        # Preserve E's actual default beat analysis. A-D are the controlled
        # branch ablation; D-vs-E intentionally compares whole pipelines.
        variants = {
            "A": combine(base, old, "A", pitch.metadata),
            "B": combine(base, [*game, *[n for n in old if n.audio_source != "vocals"]], "B", pitch.metadata),
            "C": combine(
                base, [*[n for n in old if n.audio_source == "vocals"], *background], "C", pitch.metadata
            ),
            "D": base,
            "E": original,
        }
        no_drums_folder = args.raw / f"no-drums-{case['id']}"
        no_drums = no_drums_folder / "events.json"
        receipt = no_drums_folder / "complete.json"
        if receipt.exists():
            done = json.loads(receipt.read_text())
            if done["events_sha256"] != digest(no_drums) or done["source_sha256"] != case["sha256"]:
                raise ValueError("去鼓对照不属于当前完整成功任务")
            variants["D0"] = EventResult.load(no_drums)
        else:
            error = no_drums_folder / "background/engine.stderr"
            if not error.exists():
                raise ValueError("去鼓对照缺少成功回执或失败证据")
            lines = error.read_text().splitlines()
            data["failures"].append(dict(song=case["id"], version="D0", error=lines[-1]))
        data["provenance"].append(
            dict(
                song=case["id"],
                source_sha256=case["sha256"],
                stems=base.metadata["stem_sha256"],
                hybrid=base.metadata,
                E="Frozen MuScriptor small recognition/default beat analysis on identical original bytes; not rerun timing",
                note="A/B/C/D share hybrid routing, common stems, beat reference and rendering; E is the original-mix end-to-end comparator.",
            )
        )
        for version, result in variants.items():
            if isinstance(result, EventResult):
                result.save(args.output / f"{case['id']}-{version}-events.json")
                result.export_midi(args.output / f"{case['id']}-{version}-raw.mid")
            for instrument, mode in (("Piano", "auto"), ("Violin", "auto"), ("Violin", "simple")):
                profile = instrument + ("-optional" if mode == "simple" else "")
                score, _ = build_score(result, case["title"], instrument=instrument, mode=mode)
                score.save(args.output / f"{case['id']}-{version}-{profile}.json")
                data["metrics"].append(
                    dict(song=case["id"], version=version, profile=profile, **metrics(score, case["length"]))
                )
                data["scores"][f"{case['id']}-{version}-{profile}"] = dict(
                    sections=score.root["transcription"]["sections"],
                    notes=[
                        dict(
                            t=n.time_ms,
                            d=n.duration_ms,
                            k=n.key,
                            r=n.extra.get("role", "melody"),
                            source=n.extra.get("audioSource", n.extra.get("source", "")),
                            p=n.extra.get("sourcePitch"),
                        )
                        for n in score.notes
                    ],
                )
                if not args.skip_audio:
                    for layer in ("full", "lead"):
                        notes = (
                            score.notes
                            if layer == "full"
                            else [n for n in score.notes if n.extra.get("role") != "accompaniment"]
                        )
                        y = render(notes, case["length"], instrument)
                        for w in [w for w in windows if w["song"] == case["id"]]:
                            clip = y[round(w["start"] * SAMPLE_RATE) : round(w["end"] * SAMPLE_RATE)]
                            name = f"{w['id']}-{version}-{profile}-{layer}.wav"
                            sf.write(args.output / "media" / name, clip, SAMPLE_RATE, subtype="PCM_16")
                            data["render_audit"].append(
                                dict(
                                    file=name,
                                    frames=len(clip),
                                    peak=float(abs(clip).max()),
                                    clipped_samples=int(np.sum(abs(clip) >= 0.999)),
                                )
                            )
            print(case["id"], version, flush=True)
        if not args.skip_audio:
            for source in ("original", "vocals", "background"):
                y, sr = sf.read(folder / f"{source}.wav", dtype="float32", always_2d=True)
                # Reference is never clipped silently. A common gain for all
                # reference stems is stored in the listening manifest.
                for w in [w for w in windows if w["song"] == case["id"]]:
                    clip = y[round(w["start"] * sr) : round(w["end"] * sr)] * 0.5
                    sf.write(args.output / "media" / f"{w['id']}-{source}.wav", clip, sr, subtype="PCM_24")
    if args.skip_audio:
        previous = json.loads((args.output / "comparison.json").read_text())
        if {k: v["notes"] for k, v in previous["scores"].items()} != {
            k: v["notes"] for k, v in data["scores"].items()
        }:
            raise ValueError("谱面已变化，必须重新渲染试听，不能复用旧音频")
        data["render_audit"] = previous.get("render_audit", [])
    (args.output / "comparison.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    template = Path(__file__).with_name("hybrid_review.html").read_text()
    (args.output / "index.html").write_text(
        template.replace("__COMPARISON_DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    )
    report = root / "docs/TRANSCRIPTION_HYBRID_REVIEW_2026-10-14.md"
    if report.exists():
        shutil.copy2(report, args.output / "report.md")


if __name__ == "__main__":
    main()
