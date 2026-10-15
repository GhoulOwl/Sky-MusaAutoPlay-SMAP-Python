"""Isolated, reproducible A-stage timing/memory probe (no hidden downloads)."""
from __future__ import annotations

import argparse
import json
import os
import resource
import threading
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--weights', type=Path, required=True)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--device', choices=['mps', 'cpu'], default='mps')
    parser.add_argument('--length', type=float, default=30)
    parser.add_argument('--start', type=float, default=0)
    args = parser.parse_args()
    os.environ['HF_HUB_OFFLINE'] = '1'
    import numpy as np
    import psutil
    import soundfile as sf
    import torch
    from muscriptor import TranscriptionModel
    from muscriptor.events import NoteEndEvent, ProgressEvent
    from muscriptor.utils.audio import resample

    torch.set_num_threads(4)
    torch.manual_seed(0)
    audio, sr = sf.read(args.audio, dtype='float32', always_2d=True,
                        start=0)  # File content sniffing also accepts source.audio.
    audio = audio[round(args.start * sr):round((args.start + args.length) * sr)]
    audio = resample(torch.from_numpy(np.ascontiguousarray(audio.mean(axis=1)[None])), sr, 16000)
    peak = dict(rss=0, mps_allocated=0, mps_driver=0)
    done = threading.Event()

    def sample():
        while not done.wait(.05):
            peak['rss'] = max(peak['rss'], psutil.Process().memory_info().rss)
            if args.device == 'mps':
                peak['mps_allocated'] = max(peak['mps_allocated'], torch.mps.current_allocated_memory())
                peak['mps_driver'] = max(peak['mps_driver'], torch.mps.driver_allocated_memory())

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    start = time.perf_counter()
    model = TranscriptionModel.load_model(args.weights, device=args.device)
    loaded = time.perf_counter()
    notes, chunks = [], []
    for event in model.transcribe(audio, batch_size=1, prelude_forcing=True, use_sampling=False, beam_size=1,
                                  no_eos_is_ok=False):
        if isinstance(event, NoteEndEvent):
            n = event.start_event
            notes.append(dict(pitch=n.pitch, start=n.start_time, end=event.end_time,
                              instrument=n.instrument, index=n.index))
        elif isinstance(event, ProgressEvent):
            chunks.append(dict(completed=event.completed, seconds=time.perf_counter() - loaded))
            print(json.dumps(chunks[-1]), flush=True)
    if args.device == 'mps':
        torch.mps.synchronize()
    finished = time.perf_counter()
    done.set()
    sampler.join()
    result = dict(device=args.device, torch=torch.__version__, duration=audio.shape[-1] / 16000,
                  load_seconds=loaded-start, inference_seconds=finished-loaded,
                  rtf=(finished-loaded)/(audio.shape[-1] / 16000), peak_bytes=peak,
                  maxrss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  notes=notes, chunks=chunks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k not in ('notes', 'chunks')}), flush=True)


if __name__ == '__main__':
    main()
