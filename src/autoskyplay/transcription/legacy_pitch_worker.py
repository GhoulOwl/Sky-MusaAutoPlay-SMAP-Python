"""Explicit legacy comparison on already separated float stems; never called by hybrid inference."""

from pathlib import Path

from autoskyplay.transcription.worker import ENGINE_VERSION, emit


def run(args):
    import numpy as np
    import soundfile as sf
    import torch

    torch.set_num_threads(4)
    torch.manual_seed(0)
    np.random.seed(0)
    work = Path(args.work)
    original, sr = sf.read(work / "original.wav", dtype="float32", always_2d=True)
    duration = len(original) / sr
    from autoskyplay.transcription.signal_analysis import pitch_track, rhythm_harmony

    tracks = {}
    for name, low, high in (("vocals", 60, 1500), ("other", 80, 2000), ("bass", 35, 600)):
        audio, _ = sf.read(work / (name + ".wav"), dtype="float32", always_2d=True)
        tracks[name] = pitch_track(audio, sr, emit, name, low, high)
    emit("convert", 0)
    other, _ = sf.read(work / "other.wav", dtype="float32", always_2d=True)
    bass, _ = sf.read(work / "bass.wav", dtype="float32", always_2d=True)
    drums, _ = sf.read(work / "drums.wav", dtype="float32", always_2d=True)
    analysis, bpm, estimated = rhythm_harmony(original, drums, other + bass, sr, duration)
    for name in ("other", "bass"):
        analysis.update({name + "_" + key: value for key, value in tracks[name].items()})
    import librosa

    mono = librosa.resample(original.mean(axis=1), orig_sr=sr, target_sr=16000)
    energy = librosa.feature.rms(y=mono, frame_length=1024, hop_length=160)[0][
        : len(tracks["vocals"]["frequency"])
    ]
    analysis["mix_energy_db"] = 20 * np.log10(np.maximum(energy, 1e-8))
    for name in ("vocal", "other"):
        source = tracks["vocals" if name == "vocal" else name]["energy_db"]
        analysis[name + "_ratio"] = np.clip(10 ** ((source - analysis["mix_energy_db"]) / 20), 0, 1)
    np.savez_compressed(
        work / "pitch.npz",
        **tracks["vocals"],
        **{"a_" + key: value for key, value in analysis.items()},
        hop_ms=10,
        duration_ms=duration * 1000,
        bpm=bpm,
    )
    emit(
        "done",
        100,
        duration_ms=duration * 1000,
        bpm_estimated=estimated,
        engine=ENGINE_VERSION,
        analysis_version=2,
        demucs="4.1.0",
        pitch_model="torchcrepe-full-0.0.24",
        instrument_method="dominant-pitch-conservative",
        seed=0,
        demucs_segment_seconds=4,
        pitch_batch_size=32,
    )
