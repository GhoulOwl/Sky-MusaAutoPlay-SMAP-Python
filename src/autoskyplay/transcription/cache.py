"""Seven-day download cache, independent of permanent draft source files."""
from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from ..netease import NeteaseAudio
from ..score import atomic_json

TTL = 7 * 24 * 60 * 60
_LOCK = threading.RLock()


class DownloadCache:
    def __init__(self, directory):
        self.root = Path(directory) / 'transcription' / 'downloads'
        self.root.mkdir(parents=True, exist_ok=True)

    def _entries(self):
        for path in self.root.glob('*/entry.json'):
            if path.parent.name.startswith('.'):
                continue
            try:
                data = json.loads(path.read_text('utf-8'))
                audio = NeteaseAudio(**data['audio'])
                if (not math.isfinite(data['created']) or not math.isfinite(audio.duration_ms)
                        or not 0 <= audio.duration_ms <= 86400000
                        or (audio.is_trial and not 0 <= audio.trial_start < audio.trial_end <= 86400)):
                    raise ValueError('invalid cache')
                file = path.parent / 'source.audio'
                if not file.is_file() or file.stat().st_size != data['size'] or not data['size']:
                    raise ValueError('incomplete cache')
                yield path.parent, data, audio
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def prune(self, now=None, clear=False):
        now = time.time() if now is None else now
        removed = 0
        with _LOCK:
            for directory in self.root.iterdir():
                if not directory.is_dir():
                    continue
                if directory.name.startswith('.'):
                    if clear or now - directory.stat().st_mtime >= TTL:
                        shutil.rmtree(directory)
                        removed += 1
                    continue
                try:
                    data = json.loads((directory / 'entry.json').read_text('utf-8'))
                    created = float(data['created'])
                    expired = not math.isfinite(created) or now - created >= TTL
                except (OSError, ValueError, KeyError, TypeError):
                    expired = True
                if clear or expired:
                    shutil.rmtree(directory)
                    removed += 1
        return removed

    def get(self, song_id, target=None, audio=None):
        """Copy while holding the cleanup lock; a playing source never points into the cache."""
        with _LOCK:
            self.prune()
            entries = sorted(self._entries(), key=lambda e: e[1]['created'], reverse=True)
            for directory, data, cached in entries:
                if cached.id != song_id:
                    continue
                if audio and (cached.trial_start, cached.trial_end) != (audio.trial_start, audio.trial_end):
                    continue
                if target is not None:
                    shutil.copyfile(directory / 'source.audio', target)
                return cached
        return None

    def put(self, audio, source):
        # The playback URL (which expires) and credentials are deliberately not stored.
        descriptor = asdict(audio)
        descriptor['url'] = ''
        with _LOCK:
            stage = self.root / ('.' + uuid.uuid4().hex)
            stage.mkdir()
            try:
                shutil.copyfile(source, stage / 'source.audio')
                atomic_json(stage / 'entry.json', dict(created=time.time(), audio=descriptor,
                                                      size=(stage / 'source.audio').stat().st_size))
                # Replace only the same full/trial variant after a successful download.
                for directory, _, old in list(self._entries()):
                    if (old.id, old.trial_start, old.trial_end) == (audio.id, audio.trial_start, audio.trial_end):
                        shutil.rmtree(directory)
                os.replace(stage, self.root / stage.name[1:])
            finally:
                shutil.rmtree(stage, ignore_errors=True)
