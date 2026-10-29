"""Offline MuScriptor worker. One task/model per process; no automatic downloads."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from autoskyplay.transcription.worker import emit, watch_parent  # noqa: E402


def run(args):
    import subprocess

    import imageio_ffmpeg
    import numpy as np
    import soundfile as sf
    import torch
    from muscriptor import TranscriptionModel
    from muscriptor.events import NoteEndEvent, ProgressEvent

    from autoskyplay.transcription import worker
    from autoskyplay.transcription.decoding import (
        DECODING_POLICY,
        PITCHED_INPUTS,
        pitched_model_class,
        strict_decode,
    )
    from autoskyplay.transcription.devices import device_event, select_device
    from autoskyplay.transcription.engine import MUSCRIPTOR_VERSION
    from autoskyplay.transcription.events import EventResult, ModelNote
    from autoskyplay.transcription.models import MODELS, RUNTIME_REVISION, ModelStore

    work = Path(args.work)
    request = json.loads((work / 'request.json').read_text(encoding="utf-8"))
    start, length = float(request['start']), float(request['length'])
    input_source = request.get('audio_source', 'original')
    if input_source not in ('original', 'background', 'background_no_drums', 'bass', 'guitar', 'piano', 'other'):
        raise ValueError('MuScriptor 输入来源无效')
    if input_source != 'original' and start != 0:
        raise ValueError('分轨背景已对应选段，不能再次应用原曲入点')
    if not np.isfinite([start, length]).all() or not 0 <= start <= 86400 or not 0 < length <= 600:
        raise ValueError('请选择不超过 10 分钟的有效片段')
    torch.set_num_threads(4)
    torch.manual_seed(0)
    device, device_selection = select_device(
        torch, args.device, allow_mps=True, notice=lambda message: emit('notice', message=message))
    device_event(emit, device, device_selection)
    emit('decode', 0)
    command = [imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-v', 'error', '-y', '-i', str(work/'source.audio'),
               '-ss', str(start), '-t', str(length), '-vn', '-ar', '16000', '-ac', '1',
               '-c:a', 'pcm_f32le' if input_source != 'original' else 'pcm_s16le', str(work/'original.wav')]
    worker.decoder_process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        worker.decoder_process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        worker.decoder_process.kill()
        worker.decoder_process.communicate()
        raise ValueError('音频解码超时') from None
    if worker.decoder_process.returncode:
        raise ValueError('音频无法解码或选择范围无效')
    worker.decoder_process = None
    audio, sr = sf.read(work/'original.wav', dtype='float32')
    if len(audio) < sr / 10 or not np.isfinite(audio).all():
        raise ValueError('所选范围没有可用音频')
    duration = len(audio) / sr * 1000
    model_audio, preprocessing = audio, {'policy': 'unchanged'}
    if input_source != 'original':
        from autoskyplay.transcription.background_input import numerical_tail
        model_audio, preprocessing = numerical_tail(audio, sr)
        if preprocessing['omitted_tail_ms']:
            emit('notice', message=f"背景轨末尾 {preprocessing['omitted_tail_ms']/1000:.2f} 秒低于数值噪声门限，原始浮点文件仍完整保留")
    model_duration = len(model_audio)/sr*1000
    model_class = pitched_model_class(TranscriptionModel) if input_source in PITCHED_INPUTS else TranscriptionModel
    store = ModelStore(Path(args.models).parent.parent)
    with store.lock(args.model):
        weights = store.verify(args.model)
        emit('load', message=f'加载 MuScriptor {args.model} · {device}')
        def consume(model, *, beam_size):
            notes, dropped = [], 0
            # No chunk slicing, no token-budget change and no incomplete EOS.
            for event in model.transcribe(torch.from_numpy(model_audio[None]), batch_size=1, prelude_forcing=True,
                                          use_sampling=False, beam_size=beam_size, no_eos_is_ok=False):
                if isinstance(event, NoteEndEvent):
                    n = event.start_event
                    a, b = max(0., n.start_time*1000), min(model_duration, event.end_time*1000)
                    if b > a:
                        notes.append(ModelNote(n.pitch, a, b-a, n.instrument, n.index,
                                               audio_source=input_source,
                                               event_id=f'muscriptor:{input_source}:{n.index}' if input_source != 'original' else ''))
                    else:
                        dropped += 1
                elif isinstance(event, ProgressEvent):
                    emit('pitch', int(100*event.completed/max(1, event.total)),
                         source=input_source, model=args.model, completed=event.completed, total=event.total)
            return notes, dropped

        def release(model):
            # Drop the bound model reference before the next load. The helper
            # exits the failed generator/exception scope before retrying.
            if device == 'mps':
                torch.mps.synchronize()

        (notes, dropped), attempts = strict_decode(
            lambda precision: model_class.load_model(weights, device=device, dtype=precision),
            consume, release, device=device, notice=lambda message: emit('notice', message=message))
        elapsed = sum(a['inference_seconds'] for a in attempts)
    if device == 'mps':
        torch.mps.empty_cache()
    elif device == 'cuda':
        torch.cuda.empty_cache()
    emit('convert', 0, message='整理原始音符与节拍')
    # Background events use the task's one original/drums beat reference later.
    # Their recognition cache is independent of rhythm/arrangement settings.
    bpm, valid, confidence, beats = 120., False, 0., np.array([])
    frozen_rhythm = request.get('rhythm_reference')
    if input_source == 'original' and frozen_rhythm is None:
        # Signal-only tempo estimation. No beat_this/MuseScore/soundfont model downloads.
        import librosa
        onset = librosa.onset.onset_strength(y=audio, sr=sr, hop_length=512)
        tempo, frames = librosa.beat.beat_track(onset_envelope=onset, sr=sr, hop_length=512)
        bpm = float(np.asarray(tempo).reshape(-1)[0])
        beats = librosa.frames_to_time(frames, sr=sr, hop_length=512)*1000
        beats = beats[beats < duration]
        valid = len(beats) >= 4 and 30 <= bpm <= 300
        confidence = 0.
        if valid:
            differences = np.diff(beats)
            regularity = max(0, 1-np.std(differences)/np.mean(differences)*3)
            support = min(1, np.mean(onset[frames])/max(1e-8, float(np.percentile(onset, 90))))
            confidence = float(regularity*support)
    elif frozen_rhythm is not None:
        bpm = float(frozen_rhythm['bpm'])
        beats = np.asarray(frozen_rhythm['beats_ms'], dtype=float)
        confidence = float(frozen_rhythm['beat_confidence'])
        valid = bool(frozen_rhythm.get('bpm_estimated', True))
    energy = [float(20*np.log10(max(1e-8, np.sqrt(np.mean(audio[i:i+1600]**2)))))
              for i in range(0, len(audio), 1600)]
    metadata = dict(engine='muscriptor-' + MUSCRIPTOR_VERSION, runtime_revision=RUNTIME_REVISION, model=args.model,
                    audio_source=input_source, input_origin_seconds=request.get('input_origin_seconds', start),
                    input_scale=request.get('input_scale', 1),
                    rhythm_reference='frozen-draft' if frozen_rhythm is not None else
                                     'self' if input_source == 'original' else 'shared-original-drums',
                    preprocessing=preprocessing,
                    model_repository=f'MuScriptor/muscriptor-{args.model}', model_source='modelscope.cn',
                    model_revision=MODELS[args.model]['revision'],
                    model_sha256=MODELS[args.model]['files']['model.safetensors'][1],
                    device=device, device_selection=device_selection,
                    load_seconds=sum(a['load_seconds'] for a in attempts), inference_seconds=elapsed,
                    decoding_policy=DECODING_POLICY, precision=attempts[-1]['precision'], decoding_attempts=attempts,
                    excluded_token_types=['drum'] if input_source in PITCHED_INPUTS else [],
                    batch_size=1, prelude_forcing=True, beam_size=attempts[-1]['beam_size'], sampling=False, seed=0,
                    bpm_estimated=valid, model_confidence_available=False, dropped_invalid_events=dropped,
                    warnings=['模型输出的零长度或越界音符已忽略'] if dropped else [])
    result = EventResult(notes, duration, bpm if valid else 120, metadata,
                        dict(beats_ms=beats.tolist() if valid else [], beat_confidence=confidence, energy_db=energy))
    result.save(work/'events.json')
    emit('done', 100, **metadata)


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    parser = argparse.ArgumentParser()
    parser.add_argument('--work')
    parser.add_argument('--models')
    parser.add_argument('--model', choices=['small', 'medium'], default='small')
    parser.add_argument('--device', choices=['auto', 'cpu', 'mps', 'cuda'], default='auto')
    parser.add_argument('--parent', type=int, default=0)
    parser.add_argument('--check', action='store_true')
    parser.add_argument("--probe-devices", action="store_true")
    args = parser.parse_args()
    if args.probe_devices:
        from autoskyplay.transcription.devices import emit_probe
        emit_probe(emit, args.device)
        return
    if args.check:
        import torch
        from muscriptor import TranscriptionModel

        from autoskyplay.transcription.engine import MUSCRIPTOR_VERSION
        assert callable(TranscriptionModel.load_model)
        emit('ready', engine=MUSCRIPTOR_VERSION, torch=torch.__version__, mps=torch.backends.mps.is_available(),
             cuda_runtime=torch.version.cuda, cuda_available=torch.cuda.is_available())
        return
    if not args.work or not args.models:
        parser.error('--work and --models are required')
    threading.Thread(target=watch_parent, args=(args.parent,), daemon=True).start()
    try:
        run(args)
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        from autoskyplay.transcription.decoding import read_request, worker_failure_message
        from autoskyplay.transcription.devices import error_details
        request = read_request(args.work)
        decoder_message, decoder_details = worker_failure_message(exc, request)
        message = str(exc) if isinstance(exc, ValueError) else 'MuScriptor 运行失败，请选择 CPU 或较短片段重试'
        if 'out of memory' in str(exc).lower() or isinstance(exc, MemoryError):
            message = '内存不足，请选择 small、关闭其他程序或改用 CPU 后重试'
        elif decoder_message:
            message = decoder_message
        emit('error', message=message, **dict(error_details(exc), **decoder_details))
        sys.exit(1)


if __name__ == '__main__':
    main()
