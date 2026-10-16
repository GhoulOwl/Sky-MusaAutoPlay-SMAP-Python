"""Worker-only device selection; the desktop process never imports torch."""

DEVICE_POLICY = 'cuda-probe-1'


def select_device(torch, requested='auto', *, allow_mps=False, notice=lambda message: None):
    if requested not in ('auto', 'cpu', 'cuda', 'mps'):
        raise ValueError('设备设置无效')
    details = dict(policy=DEVICE_POLICY, requested=requested, cuda_runtime=torch.version.cuda)
    if requested == 'cpu':
        return 'cpu', details
    if requested in ('auto', 'cuda'):
        try:
            if not torch.cuda.is_available():
                raise RuntimeError('未检测到可用的 CUDA 显卡或驱动')
            # Availability alone does not guarantee that this wheel supports the
            # GPU architecture/driver. Exercise allocation, kernels and cuBLAS.
            probe = torch.ones((32, 32), device='cuda')
            (probe @ probe).sum().item()
            torch.cuda.synchronize()
            del probe
            details['gpu'] = torch.cuda.get_device_name()
            return 'cuda', details
        except Exception as exc:
            details['cuda_unavailable'] = str(exc)[:500]
            if requested == 'cuda':
                raise ValueError('CUDA 无法使用，请更新 NVIDIA 驱动或选择 CPU：' + str(exc)[:200]) from exc
    if requested == 'mps' or (allow_mps and torch.backends.mps.is_available()):
        if not torch.backends.mps.is_available():
            raise ValueError('本机无法使用 MPS，请选择 CPU')
        return 'mps', details
    if torch.version.cuda:
        notice('CUDA 不可用，使用 CPU：' + details.get('cuda_unavailable', '')[:200])
    return 'cpu', details
