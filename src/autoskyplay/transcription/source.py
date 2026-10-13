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
from .cache import DownloadCache


class AudioSource:
    def __init__(self, directory, client, song, audio, local_path=None):
        base = Path(directory) / 'transcription' / 'sources'
        base.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix='source-', dir=base))
        self.client, self.song, self.audio = client, song, audio
        self.local_path = Path(local_path) if local_path else None
        self.cache = DownloadCache(directory)
        self.cached = False
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
            if self.local_path:
                if not self.local_path.is_file() or self.local_path.stat().st_size > 100 * 1024**2:
                    raise ValueError("请选择不超过 100 MB 的音频文件")
                shutil.copyfile(self.local_path, self.work / "source.audio")
            else:
                cached = self.cache.get(self.song.id, self.work / "source.audio", self.audio)
                if cached:
                    self.audio, self.cached = cached, True
            for attempt in range(0 if (self.work / "source.audio").exists() else 2):
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
            if self.cancelled.is_set():
                raise ValueError("音频准备已取消")
            if not self.local_path and not self.cached:
                self.cache.put(self.audio, self.work / "source.audio")
            data = dict(kind="local_audio" if self.local_path else "netease",
                        source_name=self.local_path.name if self.local_path else self.song.name,
                        peaks=peaks, duration=duration, origin=self.audio.trial_start or 0,
                        full_duration=max(self.song.duration_ms / 1000, self.audio.duration_ms / 1000,
                                          (self.audio.trial_start or 0) + duration),
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
