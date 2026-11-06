"""Run HOT-Step's MOSS converter with its staged tensors backed by disk."""
from __future__ import annotations

import gc
import importlib.util
import sys
import tempfile
from pathlib import Path

import numpy as np


def main():
    if len(sys.argv) != 4:
        raise SystemExit('usage: stream_moss_convert.py CONVERTER SOURCE OUTPUT')
    converter, source, output = map(Path, sys.argv[1:])
    spec = importlib.util.spec_from_file_location('hotstep_moss_converter', converter)
    if spec is None or spec.loader is None:
        raise SystemExit('Cannot load MOSS converter')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    original_put = module.Bundle.put
    with tempfile.TemporaryDirectory(prefix='.tensor-spool-', dir=output) as spool:
        count = 0

        def put(bundle, name, arr, policy):
            nonlocal count
            original_put(bundle, name, arr, policy)
            tensor_name, payload, shape, dtype = bundle.tensors[-1]
            path = Path(spool) / str(count)
            count += 1
            mapped = np.memmap(path, dtype=payload.dtype, mode='w+', shape=payload.shape)
            mapped[...] = payload
            mapped.flush()
            bundle.tensors[-1] = (tensor_name, mapped, shape, dtype)

        module.Bundle.put = put
        sys.argv = [str(converter), '--src', str(source), '--out', str(output), '--quant', 'f16']
        try:
            module.main()
        finally:
            gc.collect()


if __name__ == '__main__':
    main()
