"""Download and verify fixed six-stem, Karaoke and optional beat models."""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx

from autoskyplay.score import atomic_json
from autoskyplay.transcription.models import digest
from autoskyplay.transcription.multistem_models import ASSETS

ROOT = Path(__file__).resolve().parents[1]


def download_models(destination=None, *, with_beats=True):
    destination = Path(destination or ROOT / 'build/multistem-models')
    destination.mkdir(parents=True, exist_ok=True)
    for name, asset in ASSETS.items():
        if name == 'beat' and not with_beats:
            continue
        target = destination / asset['filename']
        if target.is_file() and target.stat().st_size == asset['size'] and digest(target) == asset['sha256']:
            continue
        temporary = target.with_suffix(target.suffix + '.part')
        try:
            with httpx.stream('GET', asset['url'], follow_redirects=True, timeout=120) as response:
                response.raise_for_status()
                written = 0
                with temporary.open('wb') as output:
                    for block in response.iter_bytes(1024**2):
                        written += len(block)
                        if written > asset['size']:
                            raise ValueError(f'{name} 模型下载大小不符')
                        output.write(block)
            if temporary.stat().st_size != asset['size'] or digest(temporary) != asset['sha256']:
                raise ValueError(f'{name} 模型校验失败')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    atomic_json(destination / 'models.json', dict(repository='adefossez/HTDemucs-6s',
                revision='3c5ee475be622df764938de97e4281a7b07ffa58',
                files={ASSETS['demucs']['filename']: ASSETS['demucs']['sha256']}))
    atomic_json(destination / 'assets.json', ASSETS)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--without-beats', action='store_true')
    args = parser.parse_args()
    print(download_models(args.destination, with_beats=not args.without_beats))
