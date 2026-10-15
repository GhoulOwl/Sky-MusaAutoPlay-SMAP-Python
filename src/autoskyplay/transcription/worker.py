"""Standalone CPU worker, also frozen as the optional offline engine executable."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

ENGINE_VERSION = "1.1.0"
decoder_process = None
runtime_profile = None


def emit(stage, progress=0, **values):
    if stage in ('done', 'error') and runtime_profile:
        values.update(runtime_profile.finish())
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
    if getattr(args, 'pitch_only', False):
        if not getattr(sys, 'frozen', False):
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from autoskyplay.transcription.legacy_pitch_worker import run as legacy_pitch
        return legacy_pitch(args)
    import imageio_ffmpeg
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.hf import load_safetensors_model

    torch.set_num_threads(min(4, os.cpu_count() or 1))
    torch.manual_seed(0)
    np.random.seed(0)
    work = Path(args.work).resolve()
    models = Path(args.models).resolve()
    verify_models(models)
    began = time.perf_counter()
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
    load_started = time.perf_counter()
    model = load_safetensors_model(models / "955717e8.safetensors").eval()
    loaded = time.perf_counter()
    # HTDemucs otherwise pads short inference chunks back to its 7.8 s training
    # length. Its supported variable-length mode bounds CPU activation memory.
    model.use_train_segment = False
    # Outer chunks bound stem allocation; the model uses overlapping inner chunks.
    block, margin = sr * 30, sr
    with ExitStack() as stack:
        outputs = {name: stack.enter_context(sf.SoundFile(work / (name + '.wav'), 'w', samplerate=sr,
                                                           channels=2, subtype='FLOAT'))
                   for name in ('vocals', 'other', 'bass', 'drums')}
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
                trim = offset - left
                count = min(block, len(original) - offset)
                for name, output in outputs.items():
                    stem = stems[0, model.sources.index(name)] * scale + mean
                    output.write(stem[:, trim:trim + count].numpy().T)
    inferred = time.perf_counter()
    del model, stems
    if getattr(args, 'separate_only', False):
        import gc
        gc.collect()
        metadata = dict(engine=ENGINE_VERSION, stage='done', progress=100, demucs='4.1.0',
                        device='cpu', seed=0, demucs_segment_seconds=4, overlap=.25,
                        outer_seconds=30, margin_seconds=1, sample_rate=sr,
                        duration_ms=duration*1000, separation_seconds=time.perf_counter()-began,
                        load_seconds=loaded-load_started, inference_seconds=inferred-loaded,
                        torch=torch.__version__, numpy=np.__version__,
                        normalization='shared mean/std per outer block; restored float stems',
                        scale=1, source_models=json.loads((models/'models.json').read_text()))
        (work/'separation.json').write_text(json.dumps(metadata))
        emit('done', **{k:v for k,v in metadata.items() if k not in ('stage','progress')})
        return
    from signal_analysis import pitch_track, rhythm_harmony

    tracks = {}
    for name, low, high in (('vocals', 60, 1500), ('other', 80, 2000), ('bass', 35, 600)):
        audio, _ = sf.read(work / (name + '.wav'), dtype='float32', always_2d=True)
        tracks[name] = pitch_track(audio, sr, emit, name, low, high)
    emit('convert', 0)
    other, _ = sf.read(work / 'other.wav', dtype='float32', always_2d=True)
    bass, _ = sf.read(work / 'bass.wav', dtype='float32', always_2d=True)
    drums, _ = sf.read(work / 'drums.wav', dtype='float32', always_2d=True)
    analysis, bpm, estimated = rhythm_harmony(original, drums, other + bass, sr, duration)
    for name in ('other', 'bass'):
        analysis.update({name + '_' + key: value for key, value in tracks[name].items()})
    import librosa
    mono = librosa.resample(original.mean(axis=1), orig_sr=sr, target_sr=16000)
    energy = librosa.feature.rms(y=mono, frame_length=1024, hop_length=160)[0][:len(tracks['vocals']['frequency'])]
    analysis['mix_energy_db'] = 20 * np.log10(np.maximum(energy, 1e-8))
    for name in ('vocal', 'other'):
        source = tracks['vocals' if name == 'vocal' else name]['energy_db']
        analysis[name + '_ratio'] = np.clip(10 ** ((source - analysis['mix_energy_db']) / 20), 0, 1)
    np.savez_compressed(work / 'pitch.npz', **tracks['vocals'],
                        **{'a_' + key: value for key, value in analysis.items()},
                        hop_ms=10, duration_ms=duration * 1000, bpm=bpm)
    emit('done', 100, duration_ms=duration * 1000, bpm_estimated=estimated,
         engine=ENGINE_VERSION, analysis_version=2, demucs='4.1.0', pitch_model='torchcrepe-full-0.0.24',
         instrument_method='dominant-pitch-conservative', seed=0, demucs_segment_seconds=4, pitch_batch_size=32)


def main():
    # JSON is consumed as UTF-8 bytes, including on Windows with a legacy locale.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--work")
    parser.add_argument("--models")
    parser.add_argument("--parent", type=int, default=0)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--separate-only", action="store_true")
    parser.add_argument("--pitch-only", action="store_true")
    args = parser.parse_args()
    if args.check:
        import demucs
        import imageio_ffmpeg
        import torchcrepe
        from signal_analysis import pitch_track, rhythm_harmony

        assert callable(pitch_track) and callable(rhythm_harmony)
        emit("ready", engine=ENGINE_VERSION, demucs=demucs.__version__,
             ffmpeg=Path(imageio_ffmpeg.get_ffmpeg_exe()).is_file(), pitch_model=torchcrepe.__name__)
        return
    if not args.work or not args.models:
        parser.error("--work and --models are required")
    threading.Thread(target=watch_parent, args=(args.parent,), daemon=True).start()
    try:
        run(args)
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
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
