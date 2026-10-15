"""Pinned ModelScope weights, independent of the seven-day song cache."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

import httpx

from ..score import atomic_json

RUNTIME_REVISION = '7f213afecf23bd6a1b8672aa223690ee9807cefb'
MODELS = {
    'small': dict(revision='dac5b012f7b9fdc5d4fd6eda846cf0c541571671', files={
        'model.safetensors': (411888600, 'bbd482c786b895cf7d8f44185073d951adae2ebb8a66f82ca84cd1f84569549c'),
        'config.json': (124, '3008fc481e4a1cd978e337eb3759260c270892204db5039235ac939e1f42aeb2')}),
    'medium': dict(revision='a29bf177425f8bc584bee29dbf84a498aeb8420f', files={
        'model.safetensors': (1228144472, 'ac80adbdf85d87231735fd948af7013441c0afced316c4e9067fd5d8a7fb97ec'),
        'config.json': (126, '43e13a70fc9ae0af36b7447c06f3eac2282daeb69d79c1ff840ede7fdaa26a3b')}),
}


def check_cancelled(cancelled):
    if cancelled():
        raise ValueError('模型操作已取消；下载可继续')


def digest(path, cancelled=lambda: False):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            check_cancelled(cancelled)
            value.update(chunk)
    return value.hexdigest()


class ModelStore:
    catalog = MODELS
    prefix = 'muscriptor'
    title = 'MuScriptor'
    weight_name = 'model.safetensors'
    license = 'CC-BY-NC-4.0'

    def repository(self, size):
        return f'MuScriptor/muscriptor-{size}'

    def remote(self, size, name):
        return (f'https://modelscope.cn/api/v1/models/{self.repository(size)}/repo',
                dict(Revision=self.catalog[size]['revision'], FilePath=name))

    def __init__(self, directory):
        self.root = Path(directory) / 'transcription' / 'models'

    def path(self, size):
        if size not in self.catalog:
            raise ValueError(f'不支持的 {self.title} 型号')
        return self.root / f'{self.prefix}-{size}-{self.catalog[size]["revision"]}'

    def installed(self, size):
        path = self.path(size)
        try:
            receipt = json.loads((path / 'verified.json').read_text())
            return receipt['revision'] == self.catalog[size]['revision'] and all(
                (path / name).stat().st_size == length
                and (path / name).stat().st_mtime_ns == receipt['files'][name]['mtime_ns']
                and receipt['files'][name]['sha256'] == sha
                for name, (length, sha) in self.catalog[size]['files'].items())
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def verify(self, size, cancelled=lambda: False):
        path = self.path(size)
        for name, (length, sha) in self.catalog[size]['files'].items():
            if not (path / name).is_file() or (path / name).stat().st_size != length:
                raise ValueError(f'{self.title} 模型缺失，请在设置中下载模型')
            if digest(path / name, cancelled) != sha:
                raise ValueError(f'{self.title} 模型校验失败，请删除后重新下载')
        return path / self.weight_name

    @contextmanager
    def lock(self, size):
        self.path(size)
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / f'.{self.prefix}-{size}.lock').open('a+b') as lock:
            if os.name == 'nt':
                import msvcrt
                lock.write(b'0')
                lock.flush()
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise ValueError('该模型正在使用或管理中，请稍后再试') from None
            else:
                import fcntl
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('该模型正在使用或管理中，请稍后再试') from None
            try:
                yield
            finally:
                if os.name == 'nt':
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def delete(self, size):
        with self.lock(size):
            shutil.rmtree(self.path(size), ignore_errors=True)
            shutil.rmtree(self.path(size).with_suffix('.partial'), ignore_errors=True)

    def install(self, size, *, source=None, cancelled=lambda: False, progress=lambda p: None, client=None):
        """Resume pinned bytes; publish only after both files pass full SHA256."""
        with self.lock(size):
            if self.installed(size):
                self.verify(size, cancelled)
                return self.path(size)
            stage = self.path(size).with_suffix('.partial')
            stage.mkdir(exist_ok=True)
            files = self.catalog[size]['files']
            total = sum(length for length, _ in files.values())
            existing = sum(min((stage / n).stat().st_size, length) for n, (length, _) in files.items()
                           if (stage / n).exists()) if source is None else 0
            if shutil.disk_usage(stage).free < total - existing + 64 * 1024**2:
                raise ValueError('磁盘空间不足，需留出模型和下载临时文件空间')
            owned = client is None
            client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, read=60))
            try:
                completed = 0
                for name, (length, sha) in files.items():
                    check_cancelled(cancelled)
                    target = stage / name
                    offset = target.stat().st_size if target.exists() else 0
                    if offset > length:
                        target.unlink()
                        offset = 0
                    if source is not None:
                        with (Path(source) / name).open('rb') as src, target.open('wb') as dst:
                            written = 0
                            while chunk := src.read(1024 * 1024):
                                check_cancelled(cancelled)
                                written += len(chunk)
                                if written > length:
                                    raise ValueError('导入模型大小不符')
                                dst.write(chunk)
                                progress(int(99 * (completed + written) / total))
                    elif offset < length:
                        url, params = self.remote(size, name)
                        headers = {'Range': f'bytes={offset}-', 'Accept-Encoding': 'identity'} if offset else {
                            'Accept-Encoding': 'identity'}
                        with client.stream('GET', url, params=params, headers=headers) as response:
                            response.raise_for_status()
                            if response.status_code == 206:
                                if response.headers.get('content-range') != f'bytes {offset}-{length - 1}/{length}':
                                    raise ValueError('下载续传范围不匹配，请重试')
                            elif response.status_code == 200:
                                offset = 0  # Server ignored Range: replace, never append duplicate bytes.
                            else:
                                raise ValueError('模型下载响应无效')
                            with target.open('ab' if offset else 'wb') as output:
                                for chunk in response.iter_bytes(1024 * 1024):
                                    check_cancelled(cancelled)
                                    offset += len(chunk)
                                    if offset > length:
                                        raise ValueError('模型下载大小不符')
                                    output.write(chunk)
                                    progress(int(99 * (completed + offset) / total))
                    if target.stat().st_size != length:
                        raise ValueError('模型下载不完整，请继续下载')
                    if digest(target, cancelled) != sha:
                        target.unlink()
                        raise ValueError('模型 SHA256 校验失败，请重新下载')
                    completed += length
                check_cancelled(cancelled)
                atomic_json(stage / 'verified.json', dict(revision=self.catalog[size]['revision'],
                    repository=self.repository(size), license=self.license,
                    files={n: dict(sha256=sha, mtime_ns=(stage / n).stat().st_mtime_ns)
                           for n, (_, sha) in files.items()}))
                if self.path(size).exists():
                    shutil.rmtree(self.path(size))
                stage.rename(self.path(size))
                progress(100)
                return self.path(size)
            finally:
                if owned:
                    client.close()
