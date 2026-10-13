"""Atomic drafts: original source, pitch data and edits are durable."""
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
from .local import MidiResult
from .melody import MelodyNote, PitchResult

AUDIO_FILES = ('original.wav', 'vocals.wav')


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

    def save(self, ident, song, result, score, params, *, edited=False, saved=False, media=None, source=None, title=None):
        ident = ident or uuid.uuid4().hex
        directory = self.path(ident)
        directory.mkdir(exist_ok=True)
        pitch = directory / 'pitch.npz'
        if isinstance(result, MidiResult):
            atomic_json(directory / 'midi-notes.json', dict(notes=[asdict(n) for n in result.raw_notes],
                                                          duration_ms=result.duration_ms, bpm=result.bpm))
        elif not pitch.exists():
            temporary = directory / 'pitch.tmp.npz'
            np.savez_compressed(temporary, **{k: getattr(result, k) for k in
                                ('frequency', 'confidence', 'energy_db', 'onset', 'hop_ms', 'duration_ms', 'bpm')})
            os.replace(temporary, pitch)
        for origin, names in ((media, ('original.wav', 'vocals.wav', 'source.audio')), (source, ('source.audio', 'source.mid', 'waveform.json'))):
            if origin:
                for name in names:
                    src, dst = Path(origin) / name, directory / name
                    if src.exists() and src.resolve() != dst.resolve() and not dst.exists():
                        temp = dst.with_suffix(dst.suffix + '.part')
                        # A crash may leave a hard-linked staging file. Unlink it before copying,
                        # so retrying cannot overwrite another draft's source through that inode.
                        temp.unlink(missing_ok=True)
                        try:
                            os.link(src, temp)
                        except OSError:
                            shutil.copyfile(src, temp)
                        os.replace(temp, dst)
        if (directory / 'source.audio').exists() and not (directory / 'waveform.json').exists():
            origin = result.metadata.get('source_origin', 0)
            duration = result.metadata.get('source_duration') or (
                result.metadata.get('source_start', 0) - origin + result.duration_ms / 1000)
            atomic_json(directory / 'waveform.json', dict(kind=result.metadata.get('source', 'netease'),
                        source_name=result.metadata.get('source_name', song.name), peaks=[], origin=origin,
                        duration=duration, full_duration=max(song.duration_ms / 1000, origin + duration),
                        trial=result.metadata.get('is_trial', False)))
        old = {}
        if (directory / 'draft.json').exists():
            old = json.loads((directory / 'draft.json').read_text('utf-8'))
        data = dict(schema=1, result_type='midi' if isinstance(result, MidiResult) else 'audio',
                    title=title or old.get('title', score.name if score else song.name), id=ident, updated=time.time(), song=asdict(song), metadata=result.metadata,
                    params=params, edited=edited, saved=saved, score=score.to_data() if score else None)
        atomic_json(directory / 'draft.json', data)
        return ident

    def load(self, ident):
        directory = self.path(ident)
        data = json.loads((directory / 'draft.json').read_text('utf-8'))
        if data.get('schema') != 1:
            raise ValueError('草稿版本不支持')
        if data.get('result_type') == 'midi':
            raw = json.loads((directory / 'midi-notes.json').read_text('utf-8'))
            result = MidiResult([MelodyNote(**note) for note in raw['notes']],
                                raw['duration_ms'], raw['bpm'], data['metadata'])
        else:
            result = PitchResult.load(directory / 'pitch.npz', data['metadata'])
        score = parse_score(json.dumps(data['score'])) if data['score'] else None
        # Touch the access time used for LRU audio eviction, not the visible edit date.
        (directory / 'draft.json').touch()
        return data, NeteaseSong(**data['song']), result, score

    def rename(self, ident, title):
        title = title.strip()
        if not title or len(title) > 120:
            raise ValueError('草稿名称需为 1—120 个字符')
        path = self.path(ident) / 'draft.json'
        data = json.loads(path.read_text('utf-8'))
        data['title'] = title
        if data.get('score'):
            data['score'][0]['name'] = title
        data['updated'] = time.time()
        atomic_json(path, data)

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
