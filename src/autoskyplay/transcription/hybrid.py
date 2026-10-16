"""Sequential, cancellable hybrid pipeline with separately keyed durable results."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import replace
from pathlib import Path

import numpy as np
import soundfile as sf

from ..score import atomic_json
from .devices import DEVICE_POLICY
from .engine import Engine
from .events import EventResult
from .game_models import GAME_MODELS, GAME_REVISION
from .models import MODELS, RUNTIME_REVISION, ModelStore, digest

HYBRID_VERSION = "hybrid-2"
STEMS = ("original.wav", "vocals.wav", "other.wav", "bass.wav", "drums.wav")


def audio_digest(path, cancelled=lambda: False):
    """Hash decoded float samples, not the WAV PEAK chunk's wall-clock timestamp."""
    value = hashlib.sha256()
    with sf.SoundFile(path) as stream:
        value.update(f"{stream.samplerate}:{stream.channels}:{len(stream)}:f32le".encode())
        while len(block := stream.read(65536, dtype="float32", always_2d=True)):
            if cancelled():
                raise ValueError("扒谱已取消")
            if not np.isfinite(block).all():
                raise ValueError("音轨包含无效样本")
            value.update(block.astype("<f4", copy=False).tobytes())
    return value.hexdigest()


def copy_file(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


class StageCache:
    """Every artifact has a digest; partial/failed stages are never cache hits."""

    def __init__(self, directory, stage, identity, cancelled=lambda: False):
        # Model manifests use tuples in Python; JSON persists them as lists.
        # Normalize before comparing so a valid completed branch is reusable.
        self.identity, self.cancelled = json.loads(json.dumps(identity, sort_keys=True)), cancelled
        self.key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.path = Path(directory) / "transcription/hybrid-cache" / stage / self.key

    def restore(self, work, expected=None):
        try:
            receipt = json.loads((self.path / "cache.json").read_text(encoding="utf-8"))
            if receipt["identity"] != self.identity:
                return False
            if expected is not None and set(receipt["files"]) != set(expected):
                return False
            for name, sha in receipt["files"].items():
                if Path(name).name != name or digest(self.path / name, self.cancelled) != sha:
                    return False
            for name in receipt["files"]:
                copy_file(self.path / name, Path(work) / name)
            (self.path / "cache.json").touch()
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def lock(self):
        locker = ModelStore.__new__(ModelStore)
        locker.root = self.path.parent
        locker.catalog = {self.key: {"revision": "cache"}}
        locker.prefix = "stage"
        return locker.lock(self.key)

    def publish(self, work, files):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stage = self.path.with_name("." + self.key + "-" + uuid.uuid4().hex)
        stage.mkdir()
        try:
            fingerprints = {}
            for name in files:
                fingerprints[name] = digest(Path(work) / name, self.cancelled)
                copy_file(Path(work) / name, stage / name)
            atomic_json(stage / "cache.json", dict(identity=self.identity, files=fingerprints))
            # A concurrent complete writer may win. Both artifacts share the same
            # full identity; never replace another task's live staging directory.
            if self.path.exists():
                # A previously corrupt entry must not make future runs rerun forever.
                old = self.path.with_name(".invalid-" + uuid.uuid4().hex)
                self.path.rename(old)
                shutil.rmtree(old, ignore_errors=True)
            stage.rename(self.path)
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def run_hybrid(task):
    config, work, directory = task.engine.config, task.work, task.directory
    origin = json.loads((work / "request.json").read_text(encoding="utf-8"))
    source_sha = digest(work / "source.audio", task.cancelled.is_set)
    models = Path(config["separation_models"])
    model_manifest = json.loads((models / "models.json").read_text(encoding="utf-8"))
    commands = config["commands"]
    timings, hits, identities, profiles = {}, {}, {}, {}
    # 3.0.0 components predate GPU separation/GAME. Keep their receipts truthful.
    stage_device = task.engine.device if config.get('accelerated_stages', True) else 'cpu'
    game_device = config.get('game_device', stage_device)

    def stage(name, identity, folder, files, runner):
        task.check_cancelled()
        identity = dict(identity, runtime_component=config.get("runtime_identity", task.engine.version))
        cache = StageCache(directory, name, identity, task.cancelled.is_set)
        identities[name] = dict(key=cache.key, identity=identity)
        started = time.perf_counter()
        profile_file = f"stage-{name}.json"
        cached_files = (*files, profile_file)
        with cache.lock():
            hits[name] = cache.restore(folder, cached_files)
            task.check_cancelled()
            if not hits[name]:
                profile = runner()
                task.check_cancelled()
                atomic_json(Path(folder) / profile_file, profile)
                cache.publish(folder, cached_files)
        profiles[name] = json.loads((Path(folder) / profile_file).read_text(encoding="utf-8"))
        timings[name] = time.perf_counter() - started

    separation = dict(
        source_sha256=source_sha,
        request=origin,
        model=model_manifest,
        runtime="demucs-4.1.0",
        worker="separate-2",
        seed=0,
        device=stage_device,
        device_policy=DEVICE_POLICY,
        segment=4,
        overlap=0.25,
        outer_seconds=30,
        margin_seconds=1,
        float=True,
    )
    task.emit("separate", message="分离音轨")
    stage(
        "separation",
        separation,
        work,
        (*STEMS, "separation.json"),
        lambda: task._run_engine(Engine(commands["separate"], models, backend='separate', device=stage_device),
                                 work, ["--separate-only"]),
    )
    stems_sha = {n: digest(work / n, task.cancelled.is_set) for n in STEMS}
    stems_pcm = {n: audio_digest(work / n, task.cancelled.is_set) for n in STEMS}
    duration = sf.info(work / "vocals.wav").duration
    features = commands.get(
        "features", [commands["separate"][0], str(Path(__file__).with_name("hybrid_features.py"))]
    )
    stage(
        "features",
        dict(stems=stems_pcm, version="hybrid-features-1"),
        work,
        ("features.json",),
        lambda: task._run_engine(Engine(features, models), work),
    )
    reference = json.loads((work / "features.json").read_text(encoding="utf-8"))
    game_work = work / "game"
    game_work.mkdir(exist_ok=True)
    copy_file(work / "vocals.wav", game_work / "vocals.wav")
    game_parameters = config.get("game", {})
    atomic_json(game_work / "request.json", dict(game=game_parameters))
    game_identity = dict(
        input_pcm_sha256=stems_pcm["vocals.wav"],
        model=GAME_MODELS["medium"],
        runtime=GAME_REVISION,
        worker="game-worker-3",
        settings=game_parameters,
        device=game_device,
        device_policy=DEVICE_POLICY,
        defaults="game-defaults-1",
    )
    task.emit("pitch", source="GAME", message="识别人声")
    stage(
        "game",
        game_identity,
        game_work,
        ("events.json",),
        lambda: task._run_engine(
            Engine(
                commands["game"], task.engine.models, backend="game", device=game_device
            ),
            game_work,
        ),
    )
    vocal = EventResult.load(game_work / "events.json")
    task.check_cancelled()
    with_drums = config.get("background_mix", "with_drums") == "with_drums"
    mix_names = ("other", "bass", "drums") if with_drums else ("other", "bass")
    background_work = work / "background"
    background_work.mkdir(exist_ok=True)
    # Floating-point files preserve over-unity peaks. No per-stem normalization.
    background = None
    for name in mix_names:
        audio, sr = sf.read(work / f"{name}.wav", dtype="float32", always_2d=True)
        background = audio if background is None else background + audio
    sf.write(work / "background.wav", background, sr, subtype="FLOAT")
    input_peak = float(np.max(np.abs(background)))
    del background, audio
    copy_file(work / "background.wav", background_work / "source.audio")
    input_source = "background" if with_drums else "background_no_drums"
    atomic_json(
        background_work / "request.json",
        dict(
            start=0,
            length=duration,
            audio_source=input_source,
            input_origin_seconds=origin["start"],
            input_scale=1,
        ),
    )
    background_sha = digest(work / "background.wav", task.cancelled.is_set)
    background_pcm = audio_digest(work / "background.wav", task.cancelled.is_set)
    background_identity = dict(
        input_pcm_sha256=background_pcm,
        combination=list(mix_names),
        model=MODELS[task.engine.model],
        size=task.engine.model,
        runtime=RUNTIME_REVISION,
        worker="background-2-near-zero-tail",
        device=task.engine.device,
        device_policy=DEVICE_POLICY,
        decoding="greedy-batch1-prelude-strict-v1",
        input_scale=1,
    )
    task.emit("pitch", source="MuScriptor", message="识别器乐")
    stage(
        "background",
        background_identity,
        background_work,
        ("events.json",),
        lambda: task._run_engine(
            Engine(
                commands["background"],
                task.engine.models,
                backend="muscriptor",
                model=task.engine.model,
                device=task.engine.device,
            ),
            background_work,
        ),
    )
    instrumental = EventResult.load(background_work / "events.json")
    if abs(vocal.duration_ms - instrumental.duration_ms) > 2:
        raise ValueError("混合分支有效范围不一致")
    task.emit("convert", message="整理混合曲谱")
    notes = [replace(n, index=i) for i, n in enumerate([*vocal.raw_notes, *instrumental.raw_notes])]
    metadata = dict(
        backend="hybrid",
        engine=HYBRID_VERSION,
        source_sha256=source_sha,
        selection=origin,
        stem_sha256=stems_sha,
        stem_pcm_sha256=stems_pcm,
        background_sha256=background_sha,
        background_pcm_sha256=background_pcm,
        background_mix=list(mix_names),
        input_scale=1,
        background_peak=input_peak,
        branches=dict(vocals=vocal.metadata, background=instrumental.metadata),
        cache_hits=hits,
        cache_identities=identities,
        timings_seconds=timings,
        stage_profiles=profiles,
        duration_ms=reference["duration_ms"],
        bpm_estimated=reference["bpm_estimated"],
    )
    analysis = dict(
        reference["analysis"],
        game_regions=vocal.analysis.get("game_regions", []),
        game_window_review=vocal.analysis.get("game_window_review", []),
    )
    result = EventResult(notes, reference["duration_ms"], reference["bpm"], metadata, analysis)
    from .hybrid_evidence import annotate_vocal_boundaries

    annotate_vocal_boundaries(result, work / "vocals.wav", task.cancelled.is_set)
    result.save(work / "events.json")
    try:
        prune_hybrid_audio(directory, protected={identities["separation"]["key"]})
    except OSError:
        task.emit("notice", message="派生分轨缓存清理未完成，可在设置中重试；识别结果已保留")
    return result


def clear_hybrid_audio(directory):
    """Evict only reproducible stems. Raw branch events and model files survive."""
    return prune_hybrid_audio(directory, budget=0)


def prune_hybrid_audio(directory, budget=1024**3, protected=()):
    base = Path(directory) / "transcription/hybrid-cache/separation"
    removed = 0
    groups = []
    for folder in base.iterdir() if base.exists() else ():
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        files = [folder / name for name in STEMS if (folder / name).is_file()]
        groups.append(((folder / "cache.json").stat().st_mtime, folder, files))
    total = sum(path.stat().st_size for _, _, files in groups for path in files)
    for _, folder, files in sorted(groups):
        if total <= budget:
            break
        if folder.name in protected:
            continue
        cache = StageCache.__new__(StageCache)
        cache.path, cache.key = folder, folder.name
        try:
            with cache.lock():
                for path in files:
                    if path.is_file():
                        removed += path.stat().st_size
                        total -= path.stat().st_size
                        path.unlink()
        except ValueError:
            continue  # A live task owns this exact stem cache.
    return removed
