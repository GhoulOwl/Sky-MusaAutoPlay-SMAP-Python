"""Standalone CPU worker, also frozen as the optional offline engine executable."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ENGINE_VERSION = "1.0.0"
decoder_process = None


def emit(stage, progress=0, **values):
    print(json.dumps(dict(stage=stage, progress=progress, **values), ensure_ascii=False), flush=True)


def watch_parent(pid):
    """Stop the decoder too when the desktop process unexpectedly disappears."""
    if not pid:
        return
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            os._exit(1)
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
    else:
        import time

        while os.getppid() == pid:
            time.sleep(.5)
    if decoder_process and decoder_process.poll() is None:
        decoder_process.kill()
    os._exit(1)


def verify_models(directory):
    manifest = json.loads((directory / "models.json").read_text())
    for name, expected in manifest["files"].items():
        path = directory / name
        if path.parent != directory or not path.is_file():
            raise ValueError("模型文件缺失，请重新安装扒谱组件")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("模型校验失败，请重新安装扒谱组件")


def run(args):
    global decoder_process
    import imageio_ffmpeg
    import librosa
    import numpy as np
    import soundfile as sf
    import torch
    import torchcrepe
    from demucs.apply import apply_model
    from demucs.hf import load_safetensors_model

    torch.set_num_threads(min(4, os.cpu_count() or 1))
    torch.manual_seed(0)
    np.random.seed(0)
    work = Path(args.work).resolve()
    models = Path(args.models).resolve()
    verify_models(models)
    request = json.loads((work / "request.json").read_text())
    start, length = float(request["start"]), float(request["length"])
    if not 0 <= start <= 86400 or not 0 < length <= 600:
        raise ValueError("请选择不超过 10 分钟的有效片段")
    emit("decode", 0)
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-y",
               "-i", str(work / "source.audio"), "-ss", str(start), "-t", str(length),
               "-vn", "-ar", "44100", "-ac", "2", "-c:a", "pcm_f32le", str(work / "original.wav")]
    decoder_process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        _, _ = decoder_process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        decoder_process.kill()
        decoder_process.communicate()
        raise ValueError("音频解码超时，请选择更短的片段") from None
    if decoder_process.returncode:
        raise ValueError("音频无法解码，或选择的片段不在可用音频范围内")
    decoder_process = None
    original, sr = sf.read(work / "original.wav", dtype="float32", always_2d=True)
    if len(original) < sr / 10 or not np.isfinite(original).all():
        raise ValueError("所选范围没有可用音频")
    duration = len(original) / sr
    if length - duration > 1:
        emit("notice", message="实际音频短于所选范围，已按可用音频处理")
    emit("separate", 0)
    model = load_safetensors_model(models / "955717e8.safetensors").eval()
    # HTDemucs otherwise pads short inference chunks back to its 7.8 s training
    # length. Its supported variable-length mode bounds CPU activation memory.
    model.use_train_segment = False
    # Outer chunks bound stem allocation; the model uses overlapping inner chunks.
    block, margin = sr * 30, sr
    with sf.SoundFile(work / "vocals.wav", "w", samplerate=sr, channels=2, subtype="FLOAT") as output:
        for offset in range(0, len(original), block):
            left, right = max(0, offset - margin), min(len(original), offset + block + margin)
            waveform = torch.from_numpy(original[left:right].T.copy())
            ref = waveform.mean(0)
            mean, scale = ref.mean(), ref.std().clamp_min(1e-8)

            def progress(info):
                position = offset + info.get("segment_offset", 0)
                emit("separate", min(99, int(100 * position / len(original))))

            with torch.inference_mode():
                stems = apply_model(model, ((waveform - mean) / scale)[None],
                                    device="cpu", shifts=0, split=True, segment=4,
                                    overlap=.25, num_workers=0, callback=progress)
                vocal = stems[0, model.sources.index("vocals")] * scale + mean
                trim = offset - left
                count = min(block, len(original) - offset)
                output.write(vocal[:, trim:trim + count].numpy().T)
    del model, stems
    vocals, _ = sf.read(work / "vocals.wav", dtype="float32", always_2d=True)
    mono = librosa.resample(vocals.mean(axis=1), orig_sr=sr, target_sr=16000)
    del vocals
    emit("pitch", 0)
    frequencies, confidences = [], []
    # Context is discarded after inference so boundaries do not duplicate notes.
    hop, block, margin = 160, 16000 * 20, 16000
    for offset in range(0, len(mono), block):
        left, right = max(0, offset - margin), min(len(mono), offset + block + margin)
        with torch.inference_mode():
            pitch, confidence = torchcrepe.predict(
                torch.from_numpy(mono[left:right].copy())[None], 16000, hop_length=hop,
                fmin=60, fmax=1500, model="full", batch_size=32, device="cpu", return_periodicity=True)
        begin = (offset - left) // hop
        count = min(block, len(mono) - offset)
        frames = (count + hop - 1) // hop
        frequencies.append(pitch[0, begin:begin + frames].numpy())
        confidences.append(confidence[0, begin:begin + frames].numpy())
        emit("pitch", min(100, int(100 * (offset + count) / len(mono))))
    frequency = np.nan_to_num(np.concatenate(frequencies), nan=0, posinf=0, neginf=0)
    confidence = np.clip(np.nan_to_num(np.concatenate(confidences)), 0, 1)
    count = len(frequency)
    energy = librosa.feature.rms(y=mono, frame_length=1024, hop_length=hop)[0]
    energy = np.pad(energy, (0, max(0, count - len(energy))), mode="edge")[:count]
    energy_db = 20 * np.log10(np.maximum(energy, 1e-8))
    onset = np.zeros(count, dtype="float32")
    attacks = librosa.onset.onset_detect(y=mono, sr=16000, hop_length=hop, backtrack=True)
    onset[attacks[attacks < count]] = 1
    emit("convert", 0)
    bpm = 120.0
    beat_estimated = False
    try:
        tempo, beats = librosa.beat.beat_track(y=original.mean(axis=1), sr=sr)
        candidate = float(np.asarray(tempo).reshape(-1)[0])
        if len(beats) >= 4 and 30 <= candidate <= 300:
            bpm, beat_estimated = candidate, True
    except (ValueError, IndexError):
        pass
    np.savez_compressed(work / "pitch.npz", frequency=frequency, confidence=confidence,
                        energy_db=energy_db, onset=onset, hop_ms=10,
                        duration_ms=duration * 1000, bpm=bpm)
    emit("done", 100, duration_ms=duration * 1000, bpm_estimated=beat_estimated,
         engine=ENGINE_VERSION, demucs="4.1.0", pitch_model="torchcrepe-full-0.0.24",
         seed=0, demucs_segment_seconds=4, pitch_batch_size=32)


def main():
    # JSON is consumed as UTF-8 bytes, including on Windows with a legacy locale.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--work")
    parser.add_argument("--models")
    parser.add_argument("--parent", type=int, default=0)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        import demucs
        import imageio_ffmpeg
        import torchcrepe

        emit("ready", engine=ENGINE_VERSION, demucs=demucs.__version__,
             ffmpeg=Path(imageio_ffmpeg.get_ffmpeg_exe()).is_file(), pitch_model=torchcrepe.__name__)
        return
    if not args.work or not args.models:
        parser.error("--work and --models are required")
    threading.Thread(target=watch_parent, args=(args.parent,), daemon=True).start()
    try:
        run(args)
    except Exception as exc:
        if isinstance(exc, MemoryError) or "out of memory" in str(exc).lower():
            message = "本机内存不足，请关闭其他程序或选择更短的片段"
        elif isinstance(exc, ValueError):
            message = str(exc)
        else:
            message = "本地扒谱引擎运行失败，请重新安装组件或选择更短的片段"
        emit("error", message=message)
        sys.exit(1)


if __name__ == "__main__":
    main()
