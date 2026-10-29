"""Offline MDX Karaoke inference on an already isolated vocal stem.

The model predicts Instrumental (backing in this input), not lead vocals.
STFT packing follows UVR's MDX convention (L real, L imag, R real, R imag).
We retain a shared amplitude scale and float outputs instead of independently
normalizing stems. No model registry, downloader or audio encoder runs here.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import soundfile as sf

from ..score import atomic_json
from .multistem_models import ASSETS, verify_asset
from .worker import emit

MDX = dict(n_fft=5120, hop=1024, bins=2048, frames=256, overlap=0.25, compensate=1.065)


def demix(audio, predict, progress=lambda value: None):
    """Overlap-add fixed model windows, retaining exact sample count and scale."""
    import torch

    if audio.ndim != 2 or audio.shape[1] != 2 or not len(audio) or not np.isfinite(audio).all():
        raise ValueError('人声分轨格式无效')
    peak = float(np.max(np.abs(audio)))
    if peak == 0:
        return np.zeros_like(audio)
    torch.set_num_threads(4)
    n_fft, hop, bins = MDX['n_fft'], MDX['hop'], MDX['bins']
    size, trim = hop * (MDX['frames'] - 1), n_fft // 2
    step = round(size * (1 - MDX['overlap']))
    source = np.pad((audio / peak).T, ((0, 0), (trim, size)))
    output, weight = np.zeros_like(source), np.zeros(source.shape[1], dtype=np.float32)
    window = torch.hann_window(n_fft)
    for start in range(0, len(audio) + trim, step):
        count = min(size, source.shape[1] - start)
        block = np.pad(source[:, start:start + count], ((0, 0), (0, size - count)))
        spectrum = torch.stft(torch.from_numpy(block), n_fft, hop, window=window,
                              center=True, return_complex=True)
        packed = torch.view_as_real(spectrum).permute(0, 3, 1, 2).reshape(1, 4, -1, MDX['frames'])
        packed = packed[:, :, :bins].contiguous().numpy()
        packed[:, :, :3] = 0
        predicted = np.asarray(predict(packed), dtype=np.float32)
        if predicted.shape != packed.shape or not np.isfinite(predicted).all():
            raise ValueError('去和声模型输出无效')
        padded = np.pad(predicted[0].reshape(2, 2, bins, -1),
                        ((0, 0), (0, 0), (0, n_fft // 2 + 1 - bins), (0, 0)))
        complex_spectrum = torch.complex(torch.from_numpy(padded[:, 0]), torch.from_numpy(padded[:, 1]))
        wave = torch.istft(complex_spectrum, n_fft, hop, window=window, center=True, length=size).numpy()
        taper = np.maximum(np.hanning(size).astype(np.float32), 1e-6)[:count]
        output[:, start:start + count] += wave[:, :count] * taper
        weight[start:start + count] += taper
        progress(min(99, int(100 * (start + count - trim) / len(audio))))
    return (output[:, trim:trim + len(audio)] / weight[trim:trim + len(audio)]
            * peak * MDX['compensate']).T.astype(np.float32)


def run(args):
    import onnxruntime as ort

    work, models = Path(args.work), Path(args.models)
    weights = verify_asset(models, 'karaoke')
    requested = args.device
    available = ort.get_available_providers()
    provider = 'CUDAExecutionProvider' if requested != 'cpu' and 'CUDAExecutionProvider' in available else 'CPUExecutionProvider'
    if requested == 'mps' or requested == 'cuda' and provider != 'CUDAExecutionProvider':
        raise ValueError('去和声阶段无法使用所选设备，请选择自动或 CPU')
    device = 'cuda' if provider.startswith('CUDA') else 'cpu'
    from . import devices
    devices.ACTIVE_DEVICE = device
    emit('device', requested=requested, actual_device=device, cached=False,
         device_selection=dict(requested=requested, actual_device=device, provider=provider))
    vocals, rate = sf.read(work / 'vocals.wav', dtype='float32', always_2d=True)
    if rate != 44100:
        raise ValueError('去和声输入必须为 44.1 kHz')
    emit('load', message=f'加载主唱去和声模型 · {device}')
    began = time.perf_counter()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    session = ort.InferenceSession(str(weights), sess_options=options, providers=[provider])
    if session.get_providers()[0] != provider:
        raise RuntimeError('去和声执行设备与请求不一致')
    loaded = time.perf_counter()
    input_name = session.get_inputs()[0].name
    backing = demix(vocals, lambda x: session.run(None, {input_name: x})[0],
                   lambda p: emit('separate', p, message='分离主唱与和声'))
    lead = vocals - backing
    if not np.isfinite(lead).all():
        raise ValueError('主唱分轨包含无效样本')
    sf.write(work / 'lead_vocals.wav', lead, rate, subtype='FLOAT')
    sf.write(work / 'backing_vocals.wav', backing, rate, subtype='FLOAT')
    metadata = dict(model=ASSETS['karaoke'], parameters=MDX, device=device,
                    device_selection=dict(requested=requested, actual_device=device, provider=provider),
                    normalization='shared input peak; restored float output',
                    primary_output='backing_vocals', lead_output='input minus compensated primary',
                    excluded_from_transcription=['backing_vocals'],
                    sample_rate=rate, duration_ms=len(vocals) / rate * 1000,
                    load_seconds=loaded - began, inference_seconds=time.perf_counter() - loaded)
    atomic_json(work / 'vocal-split.json', metadata)
    emit('done', 100, **metadata)
