"""Build the pinned, isolated MuScriptor runtime. Weights come only from ModelScope."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVISION = '7f213afecf23bd6a1b8672aa223690ee9807cefb'
ENV = ROOT / 'build/muscriptor-venv'
PYTHON = ENV / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def run(*args):
    subprocess.run([str(x) for x in args], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', action='store_true')
    parser.add_argument('--skip-install', action='store_true')
    parser.add_argument('--download', choices=['small', 'medium', 'both'])
    parser.add_argument('--data', type=Path)
    args = parser.parse_args()
    if not args.skip_install:
        if not PYTHON.exists():
            run('uv', 'venv', '--python', '3.12', ENV)
        source = ROOT / 'build/muscriptor-src'
        if not source.exists():
            run('git', 'clone', 'https://github.com/muscriptor/muscriptor.git', source)
        run('git', '-C', source, 'checkout', '--detach', REVISION)
        run('uv', 'pip', 'install', '--python', PYTHON, '--no-deps', source)
        # The library core is deliberately installed without its optional web/beat_this stack.
        run('uv', 'pip', 'install', '--python', PYTHON, '-r', ROOT/'engine/muscriptor-requirements.lock')
        (ENV/'runtime.json').write_text(json.dumps(dict(version='2.0.0', revision=REVISION)))
    worker = ROOT/'src/autoskyplay/transcription/muscriptor_worker.py'
    run(PYTHON, worker, '--check')
    if args.download:
        from autoskyplay.transcription.models import ModelStore
        if not args.data:
            parser.error('--download requires --data')
        for size in ('small', 'medium') if args.download == 'both' else (args.download,):
            print(ModelStore(args.data).install(size, progress=lambda p: print(size, p, flush=True)))
    if not args.package:
        return
    from autoskyplay.transcription.engine import platform_tag
    run(PYTHON, '-m', 'PyInstaller', '--noconfirm', '--clean', '--console',
        '--name', 'AutoSkyPlay-MuScriptor', '--paths', ROOT/'src',
        '--distpath', ROOT/'build/muscriptor-dist', '--workpath', ROOT/'build/muscriptor-pyinstaller',
        '--specpath', ROOT/'build', '--collect-data', 'imageio_ffmpeg',
        '--collect-submodules', 'librosa', '--copy-metadata', 'librosa',
        '--collect-submodules', 'scipy._external',
        '--exclude-module', 'PySide6', '--exclude-module', 'IPython', '--exclude-module', 'matplotlib', worker)
    bundle = ROOT/'build/muscriptor-dist/AutoSkyPlay-MuScriptor'
    shutil.copy2(ENV/'runtime.json', bundle/'runtime.json')
    shutil.copy2(ROOT/'THIRD_PARTY_NOTICES.md', bundle/'THIRD_PARTY_NOTICES.md')
    shutil.copy2(ROOT/'build/muscriptor-src/LICENSE', bundle/'MuScriptor-LICENSE')
    run(PYTHON, ROOT/'scripts/prepare_transcription.py', '--licenses', bundle/'licenses')
    entry = 'AutoSkyPlay-MuScriptor' + ('.exe' if os.name == 'nt' else '')
    run(bundle/entry, '--check')
    files = {}
    for file in sorted(bundle.rglob('*')):
        if file.is_file() and file.name != 'engine.json':
            with file.open('rb') as stream:
                files[file.relative_to(bundle).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
    (bundle/'engine.json').write_text(json.dumps(dict(schema=1, version='2.0.0', platform=platform_tag(),
                                                   entry=entry, files=files)))
    target = ROOT/'dist'/f'AutoSkyPlay-MuScriptor-2.0.0-{platform_tag()}.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(bundle.rglob('*')):
            if file.is_file():
                archive.write(file, file.relative_to(bundle))
    with target.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    target.with_suffix('.zip.sha256').write_text(f'{checksum}  {target.name}\n')
    print(target)


if __name__ == '__main__':
    main()
