import sys
from dataclasses import replace

import pytest
from test_transcription import fake_task
from test_transcription_devices import Probe, fake_torch

from autoskyplay.transcription.devices import error_details, probe_devices, select_device
from autoskyplay.transcription.engine import Engine
from autoskyplay.transcription.task import cached_device_event
from autoskyplay.ui.device_status import DeviceStatus


def test_all_cuda_devices_are_probed_and_best_usable_free_memory_wins():
    torch = fake_torch()
    torch.cuda.device_count = lambda: 3
    torch.cuda.mem_get_info = lambda i: ((i+1)*1024**3, 8*1024**3)
    torch.cuda.get_device_name = lambda i: f'GPU {i}'
    attempted = []

    def ones(shape, device):
        attempted.append(device)
        if device == 'cuda:2':
            raise RuntimeError('no kernel image')
        return Probe()
    torch.ones = ones
    device, report = select_device(torch)
    assert device == 'cuda' and report['actual_device'] == 'cuda:1'
    assert attempted == ['cuda:0', 'cuda:1', 'cuda:2']
    assert not report['gpus'][2]['usable'] and 'kernel' in report['gpus'][2]['reason']


def test_mps_kernel_failure_falls_back_and_explicit_cpu_skips_all_gpu_calls():
    torch = fake_torch(cuda=False, mps=True, build=None, failure='MPS unavailable')
    assert probe_devices(torch)['stages']['muscriptor'] == 'cpu'
    torch.calls.clear()
    assert select_device(torch, 'cpu')[0] == 'cpu' and not torch.calls


@pytest.mark.parametrize('message,code', [
    ('CUDA out of memory', 'accelerator_oom'),
    ('no kernel image is available', 'accelerator_incompatible'),
    ('missing model file', 'runtime_error'),
    ('did not emit EOS', 'runtime_error'),
])
def test_only_accelerator_failures_are_retryable(monkeypatch, message, code):
    monkeypatch.setattr('autoskyplay.transcription.devices.ACTIVE_DEVICE', 'cuda:1')
    assert error_details(RuntimeError(message))['error_code'] == code
    monkeypatch.setattr('autoskyplay.transcription.devices.ACTIVE_DEVICE', 'cpu')
    assert error_details(MemoryError('out of memory'))['error_code'] == 'runtime_error'


WORKER = '''
import argparse, json, pathlib, sys
p = argparse.ArgumentParser()
for name in ('work', 'models', 'parent', 'model', 'device'):
    p.add_argument('--' + name)
a = p.parse_args()
work = pathlib.Path(a.work)
assert (work/'request.json').read_text() == '{"length":123}'
if a.device == 'auto':
    (work/'partial.txt').write_text('GPU partial')
    print(json.dumps(dict(stage='error', message='CUDA out of memory',
                         actual_device='cuda:0', error_code='accelerator_oom')))
    sys.exit(1)
assert not (work/'partial.txt').exists()
(work/'complete.txt').write_text('CPU complete')
print(json.dumps(dict(stage='done', device='cpu')))
'''


def test_cpu_retry_uses_clean_directory_same_input_and_fresh_process(tmp_path):
    task = fake_task(tmp_path, WORKER)
    (task.work/'request.json').write_text('{"length":123}')
    try:
        result = task._run_engine(replace(task.engine, backend='muscriptor', device='auto'))
        assert result['device'] == 'cpu' and result['device_fallback']['attempts'] == 1
        assert (task.work/'complete.txt').read_text() == 'CPU complete'
        assert not list(task.work.glob('.cpu-retry-*'))
        assert (task.work/'engine.stderr').exists() and (task.work/'cpu-retry.stderr').exists()
    finally:
        task.close()


@pytest.mark.parametrize('requested,code', [('cpu', 'accelerator_oom'), ('cuda', 'accelerator_oom'),
                                         ('auto', 'runtime_error')])
def test_explicit_device_or_non_device_error_never_retries(tmp_path, requested, code):
    script = WORKER.replace("if a.device == 'auto':", 'if True:').replace(
        "error_code='accelerator_oom'", f"error_code='{code}'")
    task = fake_task(tmp_path, script)
    (task.work/'request.json').write_text('{"length":123}')
    try:
        with pytest.raises(ValueError):
            task._run_engine(replace(task.engine, backend='muscriptor', device=requested))
        assert not (task.work/'complete.txt').exists()
        assert not list(task.work.glob('.cpu-retry-*'))
    finally:
        task.close()


def test_cancel_before_retry_does_not_launch_cpu(tmp_path, monkeypatch):
    task = fake_task(tmp_path, WORKER)
    (task.work/'request.json').write_text('{"length":123}')
    original = task._run_engine_once

    def cancel_after_failure(*args):
        try:
            return original(*args)
        finally:
            task.cancel()
    monkeypatch.setattr(task, '_run_engine_once', cancel_after_failure)
    try:
        with pytest.raises(ValueError, match='取消'):
            task._run_engine(replace(task.engine, backend='muscriptor', device='auto'))
        assert not (task.work/'complete.txt').exists()
    finally:
        task.close()


def test_cpu_retry_failure_is_not_retried_again(tmp_path):
    task = fake_task(tmp_path, WORKER.replace("if a.device == 'auto':", 'if True:'))
    (task.work/'request.json').write_text('{"length":123}')
    try:
        with pytest.raises(ValueError):
            task._run_engine(replace(task.engine, backend='muscriptor', device='auto'))
        events = list(task.events.queue)
        assert sum(bool(e.get('fallback')) for e in events) == 1
        assert not list(task.work.glob('.cpu-retry-*'))
    finally:
        task.close()


def test_probe_widget_async_success_old_runtime_timeout_and_cached_display(qtbot, tmp_path):
    script = tmp_path/'probe.py'
    report = dict(stage='devices', schema=1, cpu={'name': 'Test CPU'}, gpus=[],
                  stages={'muscriptor': 'cpu'}, cuda_unavailable='没有 CUDA')
    script.write_text('import json\nprint(json.dumps(' + repr(report) + '))\n')
    widget = DeviceStatus()
    qtbot.addWidget(widget)
    engine = Engine([sys.executable, str(script)], tmp_path, '2.2.0', 'muscriptor')
    widget.refresh(engine, 'auto')
    qtbot.waitUntil(lambda: '预计：' in widget.summary.text())
    assert 'Test CPU' in widget.summary.text() and '没有 CUDA' in widget.summary.toolTip()
    widget.execution(dict(actual_device='cpu', cached=True, fallback={'from_device': 'cuda:0'}))
    assert '复用缓存' in widget.summary.text() and '回退' in widget.summary.text()
    cached = cached_device_event(dict(device='cpu', device_selection={
        'actual_device': 'cpu', 'cpu': {'name': 'Test CPU'}, 'cuda_unavailable': '没有 CUDA',
    }), 'auto', 'muscriptor')
    widget.execution(cached)
    assert '复用缓存：CPU' in widget.summary.text()
    assert 'Test CPU' in widget.summary.toolTip() and '没有 CUDA' in widget.summary.toolTip()
    widget.refresh(replace(engine, version='2.1.0'), 'auto')
    assert '升级' in widget.summary.text()
    script.write_text('import time\ntime.sleep(10)\n')
    widget.refresh(engine, 'auto', force=True)
    widget.timeout.start(10)
    qtbot.waitUntil(lambda: '超时' in widget.summary.text())
    widget.stop()
