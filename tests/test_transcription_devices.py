import hashlib
import json
import sys
import zipfile
from types import SimpleNamespace

import pytest

from autoskyplay.transcription.devices import select_device
from autoskyplay.transcription.engine import find_engine, install_engine, platform_tag


class Probe:
    def __matmul__(self, other):
        return self

    def sum(self):
        return self

    def item(self):
        return 32768


def fake_torch(*, cuda=True, mps=False, build='12.8', failure=None):
    calls = []

    def ones(shape, device):
        calls.append(('probe', device))
        if failure:
            raise RuntimeError(failure)
        return Probe()

    return SimpleNamespace(
        version=SimpleNamespace(cuda=build),
        cuda=SimpleNamespace(is_available=lambda: cuda, synchronize=lambda i: calls.append(('sync', i)),
                             get_device_name=lambda i: 'NVIDIA test GPU', device_count=lambda: 1,
                             set_device=lambda i: calls.append(('select', i)),
                             mem_get_info=lambda i: (8*1024**3, 12*1024**3)),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps)),
        mps=SimpleNamespace(synchronize=lambda: calls.append(('mps_sync',))),
        ones=ones, calls=calls)


def test_auto_cuda_runs_kernel_before_choosing_gpu_and_cpu_override_skips_it():
    torch = fake_torch(mps=True)
    device, details = select_device(torch, allow_mps=True)
    assert device == 'cuda' and details['gpu'] == 'NVIDIA test GPU'
    assert ('probe', 'cuda:0') in torch.calls and ('sync', 0) in torch.calls
    assert torch.calls[-1] == ('select', 0) and details['actual_device'] == 'cuda:0'
    torch.calls.clear()
    assert select_device(torch, 'cpu')[0] == 'cpu'
    assert not torch.calls


@pytest.mark.parametrize('cuda,failure', [(False, None), (True, 'no kernel image'), (True, 'driver mismatch')])
def test_unusable_cuda_falls_back_only_in_auto_mode(cuda, failure):
    torch = fake_torch(cuda=cuda, failure=failure)
    messages = []
    device, details = select_device(torch, notice=messages.append)
    assert device == 'cpu' and details['cuda_unavailable'] and messages
    with pytest.raises(ValueError, match='CUDA 无法使用'):
        select_device(torch, 'cuda')


def test_mac_keeps_muscriptor_mps_and_conservative_other_stages():
    torch = fake_torch(cuda=False, mps=True, build=None)
    assert select_device(torch, allow_mps=True)[0] == 'mps'
    assert select_device(torch)[0] == 'cpu'
    assert select_device(fake_torch(cuda=False, build=None))[0] == 'cpu'


@pytest.mark.parametrize('backend', ['separate', 'game', 'muscriptor'])
def test_worker_command_receives_requested_device(tmp_path, backend):
    from dataclasses import replace

    from test_transcription import fake_task

    task = fake_task(tmp_path, '''
import argparse, json
p = argparse.ArgumentParser()
for arg in ('work', 'models', 'parent', 'model', 'device'):
    p.add_argument('--' + arg)
a = p.parse_args()
print(json.dumps(dict(stage='done', device=a.device, model=a.model)))
''')
    try:
        engine = replace(task.engine, backend=backend, device='cuda')
        result = task._run_engine(engine)
        assert result['device'] == 'cuda'
        if backend == 'muscriptor':
            assert result['model'] == 'small'
    finally:
        task.close()


@pytest.mark.parametrize('version,backend', [('2.0.0', 'muscriptor'), ('2.1.0', 'muscriptor'),
                                          ('2.2.0', 'muscriptor'), ('3.0.0', 'hybrid'),
                                          ('3.1.0', 'hybrid'), ('3.2.0', 'hybrid')])
def test_component_upgrade_preserves_old_versions_and_runtime_cache_identity(tmp_path, version, backend):
    archive = tmp_path/'component.zip'

    def install(runtime):
        files = {'engine': b'unchanged entrypoint', 'runtime.json': runtime, 'models/models.json': b'{}'}
        manifest = dict(schema=1, version=version, platform=platform_tag(), entry='engine',
                        files={name: hashlib.sha256(data).hexdigest() for name, data in files.items()})
        with zipfile.ZipFile(archive, 'w') as package:
            package.writestr('engine.json', json.dumps(manifest))
            for name, data in files.items():
                package.writestr(name, data)
        return install_engine(archive, tmp_path/'data')

    old, new = install(b'{"torch":"cpu"}'), install(b'{"torch":"cuda"}')
    assert new.backend == backend and new.version == version
    assert old.config['runtime_identity'] != new.config['runtime_identity']
    assert find_engine(tmp_path/'data', backend) == new
    if backend == 'hybrid':
        assert new.config['accelerated_stages'] == (version != '3.0.0')


def test_windows_build_defaults_to_cuda_without_build_host_gpu(monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    from torch_runtime import install_runtime, runtime_variant, validate_runtime, verify_cuda_bundle

    assert runtime_variant(system='win32') == 'cu128'
    assert runtime_variant('cpu', system='win32') == 'cpu'
    assert runtime_variant(system='darwin') == 'native'
    calls = []
    install_runtime(lambda *args: calls.append(args), 'python', 'requirements.lock', 'cu128', torchaudio=True)
    assert 'torch==2.8.0+cu128' in calls[0] and 'torchaudio==2.8.0+cu128' in calls[0]
    assert 'https://download.pytorch.org/whl/cu128' in calls[0]
    assert calls[1][-1] == 'requirements.lock'
    info = dict(torch='2.8.0+cu128', torchaudio='2.8.0+cu128', cuda_runtime='12.8', cuda_available=False)
    validate_runtime(info, 'cu128', torchaudio=True)
    with pytest.raises(ValueError, match='CUDA'):
        validate_runtime(dict(info, cuda_runtime=None), 'cu128')
    with pytest.raises(ValueError, match='TorchAudio'):
        validate_runtime(dict(info, torchaudio='2.8.0+cpu'), 'cu128', torchaudio=True)
    monkeypatch.setattr(sys, 'platform', 'win32')
    with pytest.raises(ValueError, match='DLL'):
        verify_cuda_bundle(Path('/nonexistent-cuda-bundle'), 'cu128')
