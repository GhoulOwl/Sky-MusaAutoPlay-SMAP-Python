"""Offline GAME inference without Lightning, lyrics, ASR or an Agent."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from autoskyplay.transcription.worker import emit, watch_parent  # noqa: E402


def rest_cut(regions, start_ms, window_end_ms):
    """Choose a real decoded rest well inside context, never a note boundary."""
    candidates = [
        (r["onset_ms"] + r["offset_ms"]) / 2
        for r in regions
        if not r["voiced"]
        and r["offset_ms"] - r["onset_ms"] >= 60
        and r["onset_ms"] >= start_ms + 10000
        and r["offset_ms"] <= window_end_ms - 3000
    ]
    return max(candidates) if candidates else None


def run(args):
    import numpy as np
    import soundfile as sf
    import torch

    from autoskyplay.transcription.events import EventResult, ModelNote
    from autoskyplay.transcription.game_models import GAME_REVISION, GameModelStore
    from autoskyplay.transcription.models import digest

    if not getattr(sys, "frozen", False):
        source = (
            Path(args.game_source)
            if args.game_source
            else Path(__file__).resolve().parents[3] / "build/game-src"
        )
        sys.path.insert(0, str(source))
    from inference.me_infer import SegmentationEstimationInferenceModel
    from inference.slicer2 import Slicer
    from lib.config.io import load_raw_config
    from lib.config.schema import InferenceConfig, ModelConfig

    work = Path(args.work)
    request = json.loads((work / "request.json").read_text())
    settings = dict(
        boundary_threshold=0.2,
        presence_threshold=0.2,
        steps=8,
        radius=2,
        language="zh",
        seed=0,
        slicer_threshold=-40,
        slicer_min_length=1000,
        slicer_min_interval=200,
        slicer_silence_kept=100,
    )
    settings.update(request.get("game", {}))
    if (
        not 0 <= settings["boundary_threshold"] < 1
        or not 0 <= settings["presence_threshold"] < 1
        or not 1 <= settings["steps"] <= 64
        or not 1 <= settings["radius"] <= 10
    ):
        raise ValueError("GAME 推理参数无效")
    torch.set_num_threads(4)
    torch.manual_seed(settings["seed"])
    np.random.seed(settings["seed"])
    device = args.device
    if device == "auto":
        # CPU is the conservative initial contract; MPS is explicitly selectable
        # for compatibility probes. This is recorded, never a failure fallback.
        device = "cpu"
    audio, sr = sf.read(work / "vocals.wav", dtype="float32", always_2d=True)
    if sr != 44100 or not np.isfinite(audio).all() or not 0.1 <= len(audio) / sr <= 600:
        raise ValueError("GAME 输入必须是所选范围内的 44.1 kHz 有限浮点人声")
    mono = audio.mean(axis=1)
    store = GameModelStore(Path(args.models).parent.parent)
    with store.lock("medium"):
        path = store.verify("medium")
        beginning = time.perf_counter()
        emit("load", message=f"加载 GAME medium · {device}")
        config = load_raw_config(path.parent / "config.yaml", inherit=False)
        mc = ModelConfig.model_validate(config["model"])
        ic = InferenceConfig.model_validate(config["inference"])
        mc.check(scope_mask=0)
        ic.check(scope_mask=0)
        model = SegmentationEstimationInferenceModel(mc, ic)
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        state = checkpoint["state_dict"]
        state.update(checkpoint.get("ema_state_dict", {}))
        model.load_state_dict(state, strict=True)
        del state, checkpoint
        model = model.eval().to(device)
        language = json.loads((path.parent / "lang_map.json").read_text()).get(settings["language"])
        if language is None:
            raise ValueError("GAME 语言配置无效")
        loaded = time.perf_counter()
        chunks = Slicer(
            sr,
            threshold=settings["slicer_threshold"],
            min_length=settings["slicer_min_length"],
            min_interval=settings["slicer_min_interval"],
            max_sil_kept=settings["slicer_silence_kept"],
        ).slice(mono)
        notes, regions, slices, review = [], [], [], []
        for i, chunk in enumerate(chunks):
            wav, offset = chunk["waveform"], float(chunk["offset"])
            total = len(wav) / sr
            owned = 0.0
            while owned < total - 0.02:
                left = max(0.0, owned - 2.0)
                # Long phrases advance only through an inferred rest, with two
                # seconds of overlapping context. No note is sliced at a clock grid.
                window = min(total, owned + 35.0)
                while True:
                    piece = wav[round(left * sr) : round(window * sr)]
                    seconds = len(piece) / sr
                    if seconds < 0.05:
                        owned = total
                        break
                    emit(
                        "pitch",
                        int(100 * (offset + owned) / (len(mono) / sr)),
                        source="GAME",
                        message="识别人声",
                    )
                    with torch.inference_mode():
                        durations, presence, pitches = model(
                            waveform=torch.from_numpy(piece[None].copy()).to(device),
                            known_durations=torch.tensor([[seconds]], device=device),
                            boundary_threshold=torch.tensor(settings["boundary_threshold"], device=device),
                            boundary_radius=torch.tensor(settings["radius"], device=device),
                            score_threshold=torch.tensor(settings["presence_threshold"], device=device),
                            language=torch.tensor([language], device=device),
                            t=torch.arange(settings["steps"], device=device) / settings["steps"],
                        )
                    durations, presence, pitches = (
                        v[0].detach().cpu().numpy() for v in (durations, presence, pitches)
                    )
                    cursor = (offset + left) * 1000
                    limit = min(len(mono) / sr, offset + window) * 1000
                    observed = []
                    for duration, voiced, pitch in zip(durations, presence, pitches):
                        end = min(limit, cursor + float(duration) * 1000)
                        if end <= cursor:
                            continue
                        if not np.isfinite(pitch) or not 0 <= pitch <= 128:
                            raise ValueError("GAME 输出无效音高")
                        ident = f"game:vocals:{i}:{len(slices)}:{len(observed)}"
                        observed.append(
                            dict(
                                event_id=ident,
                                onset_ms=cursor,
                                offset_ms=end,
                                voiced=bool(voiced),
                                raw_pitch_midi=float(pitch),
                            )
                        )
                        cursor = end
                    owned_ms = (offset + owned) * 1000
                    if window >= total - 0.001:
                        cut = (offset + total) * 1000
                    else:
                        cut = rest_cut(observed, owned_ms, (offset + window) * 1000)
                        if cut is None:
                            if window < min(total, owned + 43.0):
                                window = min(total, owned + 43.0)
                                continue
                            raise ValueError(
                                "连续人声缺少可信休止切点，无法在当前内存上安全完成；未截断长音或保存残缺结果"
                            )
                    for region in observed:
                        start, end = region["onset_ms"], region["offset_ms"]
                        region["accepted"] = bool(start >= owned_ms - 0.1 and end <= cut + 0.1)
                        if region["voiced"] and start < owned_ms - 0.1 < end:
                            review.append(
                                dict(
                                    start_ms=owned_ms,
                                    end_ms=end,
                                    reason="GAME 重叠上下文在休止边界存在分歧，保留复核",
                                )
                            )
                        if region["voiced"] and region["accepted"]:
                            pitch = region["raw_pitch_midi"]
                            notes.append(
                                ModelNote(
                                    min(127, int(np.floor(pitch + 0.5))),
                                    start,
                                    end - start,
                                    "voice",
                                    len(notes),
                                    provider="game",
                                    audio_source="vocals",
                                    event_id=region["event_id"],
                                    raw_pitch_midi=pitch,
                                )
                            )
                    regions.extend(observed)
                    slices.append(
                        dict(
                            offset_ms=(offset + left) * 1000,
                            duration_ms=seconds * 1000,
                            accepted_start_ms=owned_ms,
                            accepted_end_ms=cut,
                            decoded_end_ms=cursor,
                        )
                    )
                    owned = (cut / 1000) - offset
                    break
        inference_seconds = time.perf_counter() - loaded
    if sys.platform == "win32":
        import psutil

        peak_rss = psutil.Process().memory_info().peak_wset
    else:
        import resource

        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == "darwin" else 1024
        )

    metadata = dict(
        backend="game",
        engine="game-1.0.0",
        runtime_revision=GAME_REVISION,
        model="medium",
        model_sha256=digest(path),
        input_sha256=digest(work / "vocals.wav"),
        audio_source="vocals",
        device=device,
        torch=torch.__version__,
        numpy=np.__version__,
        seed=settings["seed"],
        parameters=dict(settings, window_strategy="rest-owned-overlap-v2", max_window_seconds=45),
        load_seconds=loaded - beginning,
        inference_seconds=inference_seconds,
        slices=slices,
        model_confidence_available=False,
        peak_rss_bytes=peak_rss,
    )
    result = EventResult(
        notes, len(mono) / sr * 1000, 120, metadata, dict(game_regions=regions, game_window_review=review)
    )
    result.save(work / "events.json")
    emit("done", 100, **metadata)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.environ["HF_HUB_OFFLINE"] = "1"
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True)
    parser.add_argument("--models", required=True)
    parser.add_argument("--game-source")
    parser.add_argument("--device", choices=["auto", "cpu", "mps", "cuda"], default="auto")
    parser.add_argument("--parent", type=int, default=0)
    args = parser.parse_args()
    threading.Thread(target=watch_parent, args=(args.parent,), daemon=True).start()
    try:
        run(args)
    except Exception as exc:
        import traceback

        traceback.print_exc(file=sys.stderr)
        emit(
            "error",
            message=str(exc)
            if isinstance(exc, ValueError)
            else "GAME 运行失败，详见引擎日志；未自动更换设备",
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
