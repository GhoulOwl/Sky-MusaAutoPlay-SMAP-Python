"""Build-time PyTorch selection; Windows packages include CUDA even on CPU-only CI."""

from __future__ import annotations

import json
import subprocess
import sys

TORCH_VERSION = '2.8.0'


def runtime_variant(requested='auto', system=None):
    system = system or sys.platform
    variant = ('cu128' if system == 'win32' else 'native' if system == 'darwin' else 'cpu') if requested == 'auto' else requested
    if variant not in ('native', 'cpu', 'cu128') or (system == 'darwin' and variant == 'cu128'):
        raise ValueError('此平台不支持所选 PyTorch 运行环境')
    return 'native' if system == 'darwin' else variant


def install_runtime(run, python, requirements, variant, *, torchaudio=False):
    if variant != 'native':
        packages = ['torch'] + (['torchaudio'] if torchaudio else [])
        # Install the exact accelerator build first. The public version in the
        # lock accepts its +cu128/+cpu suffix; uv retains satisfying installs.
        run('uv', 'pip', 'install', '--python', python, '--no-deps',
            '--index-url', f'https://download.pytorch.org/whl/{variant}',
            *(f'{name}=={TORCH_VERSION}+{variant}' for name in packages))
    run('uv', 'pip', 'install', '--python', python, '-r', requirements)


def inspect_runtime(python, variant, *, torchaudio=False):
    script = (
        'import json, torch; '
        'print(json.dumps(dict(torch=torch.__version__, cuda_runtime=torch.version.cuda, '
        'cuda_available=torch.cuda.is_available()'
        + (", torchaudio=__import__('torchaudio').__version__" if torchaudio else '') + ')))'
    )
    info = json.loads(subprocess.check_output([str(python), '-c', script], text=True, encoding='utf-8'))
    validate_runtime(info, variant, torchaudio=torchaudio)
    return dict(info, variant=variant)


def validate_runtime(info, variant, *, torchaudio=False):
    # CI does not need an NVIDIA GPU, but must never publish CPU torch as CUDA.
    if info['torch'].split('+')[0] != TORCH_VERSION:
        raise ValueError('PyTorch 版本与固定运行环境不符')
    if variant == 'cu128' and info['cuda_runtime'] != '12.8':
        raise ValueError('CUDA 12.8 运行库缺失，拒绝发布 CPU 替代包')
    if variant == 'cpu' and info['cuda_runtime'] is not None:
        raise ValueError('CPU 组件意外包含 CUDA 运行库')
    if torchaudio and info['torchaudio'] != info['torch']:
        raise ValueError('TorchAudio 与 PyTorch 版本或加速环境不匹配')


def verify_cuda_bundle(bundle, variant):
    if variant == 'cu128' and sys.platform == 'win32':
        names = [path.name.lower() for path in bundle.rglob('*.dll')]
        for prefix in ('torch_cuda', 'cudart64', 'cublas64', 'cudnn'):
            if not any(name.startswith(prefix) for name in names):
                raise ValueError(f'Windows CUDA 组件缺少 {prefix} DLL')
