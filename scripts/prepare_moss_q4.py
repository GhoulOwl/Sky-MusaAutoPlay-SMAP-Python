"""Convert the verified ModelScope MOSS source using a local HOT-Step toolchain."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from autoskyplay.transcription.moss_models import MossSourceStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True, help='AutoSkyPlay data directory')
    parser.add_argument('--converter', type=Path, required=True, help='HOT-Step engine/tools/convert-moss.py')
    parser.add_argument('--quantizer', type=Path, required=True, help='HOT-Step quantize executable')
    parser.add_argument('--output', type=Path, required=True, help='new output directory')
    args = parser.parse_args()
    store = MossSourceStore(args.data_dir)
    if not args.converter.is_file() or not args.quantizer.is_file():
        parser.error('converter or quantizer is missing')
    if args.output.exists():
        parser.error('output already exists; choose a new directory')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with store.lock('moss-source'), tempfile.TemporaryDirectory(prefix='.moss-convert-', dir=args.output.parent) as temp:
        store.verify('moss-source')
        stage = Path(temp)
        subprocess.run([sys.executable, str(Path(__file__).with_name('stream_moss_convert.py')),
                        str(args.converter.resolve()), str(store.path('moss-source').resolve()), str(stage.resolve())],
                       check=True)
        source = stage / 'moss-lm-f16.gguf'
        target = stage / 'moss-lm-q4_k_m.gguf'
        subprocess.run([str(args.quantizer.resolve()), str(source), str(target), 'Q4_K_M'], check=True)
        if not target.is_file() or not (stage / 'moss-aud-f16.gguf').is_file():
            raise RuntimeError('conversion did not produce both GGUF files')
        source.unlink()
        os.replace(stage, args.output)
    print('Q4 model prepared:', args.output)


if __name__ == '__main__':
    main()
