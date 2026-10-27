"""Device protocol shared by workers; importing this module never imports torch."""
from __future__ import annotations

import os
import platform
import subprocess

DEVICE_POLICY = 'stage-probe-2'
ACTIVE_DEVICE = 'cpu'


def cpu_info():
    name = platform.processor() or platform.machine()
    try:
        if platform.system() == 'Darwin':
            name = subprocess.check_output(
                ['/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string'], timeout=2, text=True).strip()
        elif platform.system() == 'Windows':
            name = os.environ.get('PROCESSOR_IDENTIFIER', name)
    except (OSError, subprocess.SubprocessError):
        pass
    return dict(name=name, architecture=platform.machine(), logical_cores=os.cpu_count() or 1)


def probe_devices(torch, requested='auto'):
    if requested not in ('auto', 'cpu', 'cuda', 'mps'):
        raise ValueError('设备设置无效')
    report = dict(schema=1, policy=DEVICE_POLICY, requested=requested, cpu=cpu_info(),
                  cuda_runtime=torch.version.cuda, gpus=[], cuda_unavailable='', mps_unavailable='')
    if requested == 'cpu':
        report['stages'] = {s: 'cpu' for s in ('separate', 'game', 'background', 'muscriptor')}
        return report
    try:
        if not torch.cuda.is_available():
            raise RuntimeError('当前组件或驱动未提供可用 CUDA')
        for index in range(torch.cuda.device_count()):
            gpu = dict(device=f'cuda:{index}', index=index, name=f'CUDA GPU {index}', usable=False)
            try:
                torch.cuda.set_device(index)
                gpu['name'] = torch.cuda.get_device_name(index)
                probe = torch.ones((32, 32), device=f'cuda:{index}')
                (probe @ probe).sum().item()
                torch.cuda.synchronize(index)
                del probe
                free, total = torch.cuda.mem_get_info(index)
                gpu.update(usable=True, free_memory=int(free), total_memory=int(total))
            except Exception as exc:
                gpu['reason'] = str(exc)[:500]
            report['gpus'].append(gpu)
    except Exception as exc:
        report['cuda_unavailable'] = str(exc)[:500]
    usable = [g for g in report['gpus'] if g['usable']]
    if not usable and not report['cuda_unavailable']:
        report['cuda_unavailable'] = '; '.join(g.get('reason', '') for g in report['gpus'])
    try:
        if not torch.backends.mps.is_available():
            raise RuntimeError('当前系统或组件不支持 MPS')
        probe = torch.ones((32, 32), device='mps')
        (probe @ probe).sum().item()
        torch.mps.synchronize()
        del probe
        report['gpus'].append(dict(device='mps', name=f"{report['cpu']['name']} GPU · MPS", usable=True))
    except Exception as exc:
        report['mps_unavailable'] = str(exc)[:500]
    best = max(usable, key=lambda g: (g['free_memory'], -g['index'])) if usable else None
    cuda = best['device'] if best else 'cpu'
    mps = any(g['device'] == 'mps' and g['usable'] for g in report['gpus'])
    report['stages'] = dict(separate=cuda, game=cuda,
                            background=cuda if best else 'mps' if mps else 'cpu',
                            muscriptor=cuda if best else 'mps' if mps else 'cpu')
    report['limitations'] = {'mps': 'Demucs / GAME 尚未启用 MPS；这些阶段使用 CPU'}
    return report


def select_device(torch, requested='auto', *, allow_mps=False, notice=lambda message: None):
    global ACTIVE_DEVICE
    report = probe_devices(torch, requested)
    device = report['stages']['muscriptor' if allow_mps else 'separate']
    if requested == 'cuda' and not device.startswith('cuda'):
        raise ValueError('CUDA 无法使用，请更新 NVIDIA 驱动或选择 CPU：' + report['cuda_unavailable'][:200])
    if requested == 'mps':
        if not allow_mps or report['mps_unavailable']:
            raise ValueError('本阶段无法使用 MPS，请选择自动或 CPU')
        device = 'mps'
    if device.startswith('cuda'):
        torch.cuda.set_device(int(device.split(':')[1]))
        report['gpu'] = next(g['name'] for g in report['gpus'] if g['device'] == device)
        # Model libraries also use the default CUDA device internally.
        selected = 'cuda'
    else:
        selected = device
    report['actual_device'] = device
    ACTIVE_DEVICE = device
    if requested == 'auto' and selected == 'cpu' and torch.version.cuda:
        notice('CUDA 不可用，使用 CPU：' + report['cuda_unavailable'][:200])
    return selected, report


def device_event(emit, device, selection):
    emit('device', requested=selection['requested'], actual_device=selection.get('actual_device', device),
         device_selection=selection, cached=False)


def error_details(exc):
    """Only explicit accelerator failures qualify for a CPU retry."""
    message = str(exc).lower()
    accelerator = ACTIVE_DEVICE.startswith('cuda') or ACTIVE_DEVICE == 'mps'
    oom = 'out of memory' in message
    compatibility = any(t in message for t in (
        'no kernel image', 'invalid device function', 'cuda driver', 'driver version is insufficient',
        'not implemented for the mps', 'not currently implemented for the mps',
        'not supported on mps', "not implemented for 'mps'", 'cublas_status_not_supported'))
    return dict(actual_device=ACTIVE_DEVICE, device_error=str(exc)[:500],
                error_code='accelerator_oom' if accelerator and oom else
                'accelerator_incompatible' if accelerator and compatibility else 'runtime_error')


def emit_probe(emit, requested='auto'):
    import torch

    emit('devices', **probe_devices(torch, requested))
