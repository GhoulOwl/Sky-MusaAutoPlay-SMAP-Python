"""Atomic, durable drafts. Audio is disposable; pitch data and edits are not."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import numpy as np

from ..netease import NeteaseSong
from ..score import atomic_json, parse_score
from .melody import PitchResult

AUDIO_FILES = ('source.audio', 'original.wav', 'vocals.wav')


class DraftStore:
    def __init__(self, directory):
        self.root = Path(directory) / 'transcription' / 'drafts'
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, ident):
        if not re.fullmatch(r'[a-f0-9]{32}', ident):
            raise ValueError('草稿 ID 无效')
        return self.root / ident

    def entries(self):
        entries = []
        for path in self.root.glob('*/draft.json'):
            try:
                data = json.loads(path.read_text('utf-8'))
                if self.path(data['id']) == path.parent and data['schema'] == 1:
                    entries.append(data)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(entries, key=lambda d: d['updated'], reverse=True)

    def save(self, ident, song, result, score, params, *, edited=False, saved=False, media=None, source=None):
        ident = ident or uuid.uuid4().hex
        directory = self.path(ident)
        directory.mkdir(exist_ok=True)
        pitch = directory / 'pitch.npz'
        if not pitch.exists():
            temporary = directory / 'pitch.tmp.npz'
            np.savez_compressed(temporary, **{k: getattr(result, k) for k in
                                ('frequency', 'confidence', 'energy_db', 'onset', 'hop_ms', 'duration_ms', 'bpm')})
            os.replace(temporary, pitch)
        for origin, names in ((media, ('original.wav', 'vocals.wav')), (source, ('source.audio', 'waveform.json'))):
            if origin:
                for name in names:
                    src, dst = Path(origin) / name, directory / name
                    if src.exists() and src.resolve() != dst.resolve() and not dst.exists():
                        temp = dst.with_suffix(dst.suffix + '.part')
                        try:
                            os.link(src, temp)
                        except OSError:
                            shutil.copyfile(src, temp)
                        os.replace(temp, dst)
        data = dict(schema=1, id=ident, updated=time.time(), song=asdict(song), metadata=result.metadata,
                    params=params, edited=edited, saved=saved, score=score.to_data() if score else None)
        atomic_json(directory / 'draft.json', data)
        return ident

    def load(self, ident):
        directory = self.path(ident)
        data = json.loads((directory / 'draft.json').read_text('utf-8'))
        if data.get('schema') != 1:
            raise ValueError('草稿版本不支持')
        result = PitchResult.load(directory / 'pitch.npz', data['metadata'])
        score = parse_score(json.dumps(data['score'])) if data['score'] else None
        # Touch the access time used for LRU audio eviction, not the visible edit date.
        (directory / 'draft.json').touch()
        return data, NeteaseSong(**data['song']), result, score

    def delete(self, ident):
        shutil.rmtree(self.path(ident))

    def prune_audio(self, budget=1024**3, protected=()):
        groups = []
        for entry in self.entries():
            directory = self.path(entry['id'])
            files = [directory / name for name in AUDIO_FILES if (directory / name).exists()]
            groups.append(((directory / 'draft.json').stat().st_mtime, entry['id'], files))
        total = sum(p.stat().st_size for _, _, files in groups for p in files)
        for _, ident, files in sorted(groups):
            if total <= budget:
                break
            if ident in protected:
                continue
            for path in files:
                total -= path.stat().st_size
                path.unlink(missing_ok=True)
        return total
