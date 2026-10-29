"""Pinned multistem assets. Downloads belong to preparation, never inference."""

from __future__ import annotations

from pathlib import Path

from .models import digest

ASSETS = {
    'demucs': dict(
        filename='5c90dfd2.safetensors', size=54885744,
        sha256='d2a1745f0744721f6b8ca5bf469b67c651ea5ed1b52998cab033b2158609d411',
        url='https://huggingface.co/adefossez/HTDemucs-6s/resolve/'
            '3c5ee475be622df764938de97e4281a7b07ffa58/5c90dfd2.safetensors',
        license='MIT',
    ),
    'karaoke': dict(
        filename='UVR_MDXNET_KARA_2.onnx', size=52786726,
        sha256='bf32e15105a09c0f7dddd2b67346146334d6f3ecb399ed7638eba2ab07cbf5f4',
        url='https://github.com/TRvlvr/model_repo/releases/download/'
            'all_public_uvr_models/UVR_MDXNET_KARA_2.onnx',
        license='UVR model terms; see THIRD_PARTY_NOTICES.md',
    ),
    'beat': dict(
        filename='beat_this-final0.ckpt', size=81058141,
        sha256='8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331',
        url='https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/final0.ckpt',
        license='MIT',
    ),
}


def verify_asset(directory, name, cancelled=lambda: False):
    asset = ASSETS[name]
    path = Path(directory) / asset['filename']
    if not path.is_file() or path.stat().st_size != asset['size']:
        raise ValueError(f'六轨组件缺少 {name} 模型，请运行准备脚本或重新导入 4.0.0 组件')
    if digest(path, cancelled) != asset['sha256']:
        raise ValueError(f'六轨组件 {name} 模型校验失败，请重新准备组件')
    return path
