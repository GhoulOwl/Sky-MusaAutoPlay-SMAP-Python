"""Download and reduce real audio to a bounded waveform before inference."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from ..score import atomic_json


class AudioSource:
    def __init__(self, directory, client, song, audio):
        base = Path(directory) / 'transcription' / 'sources'
        base.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix='source-', dir=base))
        self.client, self.song, self.audio = client, song, audio
        self.cancelled = threading.Event()
        self.completed = threading.Event()
        self.started = threading.Event()
        self.disposed = threading.Event()
        self.lock = threading.Lock()

    def close(self):
        self.cancelled.set()
        with self.lock:
            self.disposed.set()
            if self.completed.is_set() or not self.started.is_set():
                shutil.rmtree(self.work, ignore_errors=True)

    def prepare(self):
        with self.lock:
            self.started.set()
            if self.disposed.is_set():
                raise ValueError("音频准备已取消")
        try:
            for attempt in range(2):
                try:
                    self.client.download_audio(self.audio, self.work / 'source.audio', self.cancelled.is_set)
                    break
                except ValueError:
                    if self.cancelled.is_set() or attempt:
                        raise
                    updated = self.client.playback_audio(self.song.id)
                    if (updated.trial_start, updated.trial_end) != (self.audio.trial_start, self.audio.trial_end):
                        raise ValueError('歌曲可用范围已变化，请重新选择歌曲') from None
                    self.audio = updated
            peaks = []
            with sf.SoundFile(self.work / 'source.audio') as stream:
                duration = len(stream) / stream.samplerate
                if not .1 <= duration <= 86400:
                    raise ValueError('音频时长无效')
                block = max(1, int(stream.samplerate * max(.02, duration / 30000)))
                for samples in stream.blocks(blocksize=block, dtype='float32', always_2d=True):
                    if self.cancelled.is_set():
                        raise ValueError('音频准备已取消')
                    peaks.append(float(np.clip(np.max(np.abs(samples)), 0, 1)))
            data = dict(peaks=peaks, duration=duration, origin=self.audio.trial_start or 0,
                        full_duration=max(self.song.duration_ms / 1000, self.audio.duration_ms / 1000),
                        trial=self.audio.is_trial)
            atomic_json(self.work / 'waveform.json', data)
            return data
        finally:
            with self.lock:
                self.completed.set()
                if self.disposed.is_set():
                    shutil.rmtree(self.work, ignore_errors=True)


def load_waveform(directory):
    path = Path(directory) / 'waveform.json'
    return json.loads(path.read_text('utf-8')) if path.exists() else None
