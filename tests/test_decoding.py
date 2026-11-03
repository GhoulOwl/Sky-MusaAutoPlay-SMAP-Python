import json
from dataclasses import replace

import pytest
from test_transcription import fake_task

from autoskyplay.transcription.decoding import (
    eos_failure,
    read_request,
    strict_decode,
    worker_failure_message,
)
from autoskyplay.transcription.task import EngineFailure

EOS = 'chunk 16 (seek=80.0s) did not emit EOS within 2000 tokens (this is only raised under --strict-eos)'


def test_precision_retry_restarts_complete_input_and_discards_failed_events():
    loaded, released, notices, seen = [], [], [], []
    complete_input = list(range(49))

    def load(precision):
        loaded.append(precision)
        return precision

    def consume(model, *, beam_size):
        events = []
        seen.append(complete_input)
        for chunk in complete_input:
            events.append((model, chunk))
            if model == 'float16' and chunk == 16:
                raise RuntimeError(EOS)
        return events

    events, attempts = strict_decode(load, consume, released.append, device='mps', notice=notices.append)
    assert loaded == released == ['float16', 'float32']
    assert len(seen) == 2 and all(item is complete_input for item in seen)
    assert events == [('float32', c) for c in complete_input]
    assert len(notices) == 1 and 'FP32' in notices[0]
    assert [a['completed'] for a in attempts] == [False, True]
    assert attempts[0]['decoder_failure']['token_limit'] == 2000


@pytest.mark.parametrize('device,error,precisions', [
    ('cpu', EOS, ['float32', 'float32']), ('cuda', EOS, ['float32', 'float32']),
    ('mps', 'out of memory', ['float16']), ('mps', EOS, ['float16', 'float32', 'float32'])])
def test_retry_is_bounded_and_only_for_strict_eos(device, error, precisions):
    loaded, released = [], []

    def load(precision):
        loaded.append(precision)
        return precision

    def consume(model, *, beam_size):
        raise RuntimeError(error)

    with pytest.raises(RuntimeError, match=error.split('(')[0].strip()):
        strict_decode(load, consume, released.append, device=device)
    assert loaded == released == precisions


def test_precise_error_uses_actual_source_and_original_time():
    message, details = worker_failure_message(RuntimeError(EOS), dict(audio_source='piano', input_origin_seconds=40))
    assert '钢琴轨' in message and '02:00.0' in message and '2000' in message
    assert details['error_code'] == 'decoder_incomplete'
    assert details['decoder_failure']['chunk'] == 16
    assert eos_failure(RuntimeError('not an EOS failure')) is None


@pytest.mark.parametrize('model,action', [('small', '请手动切换到 medium'), ('medium', '手动调整模型或所选范围')])
def test_decoder_failure_prompts_manual_model_change_without_recommending_current_model(model, action):
    message, _ = worker_failure_message(RuntimeError(EOS), model=model, cpu_retry_exhausted=True)
    assert action in message
    assert '已自动切换 CPU 重试但仍未完成' in message and '可尝试 CPU' not in message


def test_native_beam_retry_is_last_bounded_attempt_with_full_input():
    attempts_seen = []
    full_audio = object()

    def consume(model, *, beam_size):
        attempts_seen.append((model, beam_size, full_audio))
        if beam_size == 1:
            raise RuntimeError(EOS)
        return ['complete']

    output, attempts = strict_decode(lambda precision: precision, consume, lambda model: None, device='mps')
    assert output == ['complete']
    assert [(p, b) for p, b, _ in attempts_seen] == [('float16', 1), ('float32', 1), ('float32', 2)]
    assert all(a is full_audio for _, _, a in attempts_seen)
    assert [a['completed'] for a in attempts] == [False, False, True]


def test_diagnostic_parsing_cannot_replace_original_error(tmp_path):
    (tmp_path / 'request.json').write_text('invalid json')
    assert read_request(tmp_path) == {}
    message, _ = worker_failure_message(RuntimeError(EOS), dict(audio_source=[], input_origin_seconds=None))
    assert '01:20.0' in message


def test_legacy_generic_error_exposes_decoder_and_persists_logs_after_close(tmp_path):
    task = fake_task(tmp_path, '''
import argparse, json, sys
p=argparse.ArgumentParser()
for name in ('work','models','parent','model','device'):p.add_argument('--'+name)
a=p.parse_args()
sys.stderr.write('RuntimeError: chunk 16 (seek=80.0s) did not emit EOS within 2000 tokens\\n')
print(json.dumps(dict(stage='error',message='混合后端阶段失败，详见阶段日志',actual_device='mps',error_code='runtime_error')))
sys.exit(1)
''')
    task.engine = replace(task.engine, backend='muscriptor')
    task.start()
    task.thread.join(10)
    events = list(task.events.queue)
    error = next(e for e in events if e['stage'] == 'error')
    assert '2000' in error['message'] and task.result is None
    from pathlib import Path
    diagnostic = Path(error['diagnostic_path'])
    report = json.loads(diagnostic.read_text(encoding='utf-8'))
    assert report['error_code'] == 'decoder_incomplete'
    task.close()
    assert not task.work.exists() and diagnostic.is_file()
    assert 'did not emit EOS' in (diagnostic.parent / 'engine.stderr').read_text()


def test_pitched_decoder_masks_only_drum_tokens_and_preserves_native_controls(monkeypatch):
    import sys
    from types import SimpleNamespace

    from autoskyplay.transcription.decoding import pitched_model_class

    class Tensor(list):
        def unique(self):
            return Tensor(sorted(set(self)))

    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(
        tensor=lambda ids, **kwargs: Tensor(ids), cat=lambda arrays: Tensor(sum((list(a) for a in arrays), [])),
        long='int64'))

    class Base:
        def __init__(self):
            self._device = 'mps'
            self._tokenizer = SimpleNamespace(_vocab=[SimpleNamespace(type=t) for t in
                  ('EOS', 'pitch', 'program', 'drum', 'velocity', 'drum', 'shift', 'tie')])

        def _generate_token_stream(self, *args):
            return args

    model = pitched_model_class(Base)()
    conditions, seek = object(), [0, 5, 10]
    args = model._generate_token_stream(conditions, seek, 1, 2000, False, 1., 1., False, True, 1)
    assert args[0] is conditions and args[1] is seek
    assert args[2:-1] == (1, 2000, False, 1., 1., False, True, 1)
    assert args[-1] == [3, 5]
    assert model._generate_token_stream(conditions, seek, 1, 2000, False, 1., 1., False,
                                        forbidden_tokens=Tensor([1]))[-1] == [1, 3, 5]


RECOVERY_WORKER = '''
import argparse, json, pathlib, sys
from autoskyplay.transcription.events import EventResult, ModelNote
p = argparse.ArgumentParser()
for name in ('work', 'models', 'parent', 'model', 'device'):
    p.add_argument('--' + name, default='small' if name == 'model' else None)
a = p.parse_args()
work = pathlib.Path(a.work)
assert a.model == 'small'
assert (work/'source.audio').read_bytes() == b'complete original audio'
assert json.loads((work/'request.json').read_text()) == dict(
    start=0, length=94.85, audio_source='other', input_origin_seconds=193.27)
error = 'chunk 11 (seek=55.0s) did not emit EOS within 2000 tokens'
if a.device == 'auto':
    (work/'partial.json').write_text('MPS partial')
    (work/'events.json').write_text('MPS incomplete output')
    sys.stderr.write('MPS decoding failed: ' + error)
    print(json.dumps(dict(stage='error', message=error, actual_device='mps', error_code='decoder_incomplete',
        device_error=error, decoding_attempts=[dict(precision='float32', beam_size=2, completed=False)])))
    sys.exit(1)
assert a.device == 'cpu'
assert not (work/'partial.json').exists() and not (work/'events.json').exists()
sys.stderr.write('CPU complete')
metadata = dict(device='cpu', model=a.model, decoding_attempts=[dict(precision='float32', completed=True)])
EventResult([ModelNote(60, 0, 1000, 'acoustic_piano', 0)], 94850, metadata=metadata).save(work/'events.json')
print(json.dumps(dict(stage='done', **metadata)))
'''


def recovery_task(tmp_path, script=RECOVERY_WORKER):
    task = fake_task(tmp_path, script)
    task.engine = replace(task.engine, backend='muscriptor', device='auto')
    (task.work/'request.json').write_text(json.dumps(dict(
        start=0, length=94.85, audio_source='other', input_origin_seconds=193.27)))
    (task.work/'source.audio').write_bytes(b'complete original audio')
    return task


def test_mps_decoder_failure_keeps_selected_model_and_prompts_manual_switch(tmp_path):
    task = recovery_task(tmp_path)
    try:
        with pytest.raises(EngineFailure) as failure:
            task._run_engine()
        assert '请手动切换到 medium' in str(failure.value)
        assert failure.value.details['error_code'] == 'decoder_incomplete'
        assert task.engine.model == 'small'
        assert 'MPS decoding failed' in (task.work/'engine.stderr').read_text()
        assert not (task.work/'cpu-retry.stderr').exists()
        assert not list(task.work.glob('.cpu-retry-*'))
        assert not any(e.get('fallback') for e in task.events.queue)
    finally:
        task.close()


@pytest.mark.parametrize('requested,actual,backend', [
    ('auto', 'mps', 'muscriptor'),
    ('cpu', 'mps', 'muscriptor'), ('mps', 'mps', 'muscriptor'), ('cuda', 'mps', 'muscriptor'),
    ('auto', 'cpu', 'muscriptor'), ('auto', 'cuda', 'muscriptor'), ('auto', 'mps', 'game'),
])
def test_decoder_error_never_changes_model_or_retries_on_another_device(tmp_path, requested, actual, backend):
    script = RECOVERY_WORKER.replace("if a.device == 'auto':", 'if True:').replace(
        "actual_device='mps'", f"actual_device='{actual}'")
    task = recovery_task(tmp_path, script)
    try:
        with pytest.raises(EngineFailure) as failure:
            task._run_engine(replace(task.engine, backend=backend, device=requested))
        assert failure.value.details['error_code'] == 'decoder_incomplete'
        assert failure.value.details['actual_device'] == actual
        assert not any(e.get('fallback') for e in task.events.queue)
        assert not (task.work/'cpu-retry.stderr').exists()
    finally:
        task.close()


def test_failed_cpu_decoder_retry_is_bounded_and_both_attempts_remain_diagnosable(tmp_path):
    # Existing OOM recovery still uses the same model on CPU. If that decoder
    # fails too, retain both logs and give the manual model-switch prompt.
    script = RECOVERY_WORKER.replace("if a.device == 'auto':", "if a.device == 'auto':\n    error = 'MPS out of memory'")
    script = script.replace("error_code='decoder_incomplete',\n        device_error=error",
                            "error_code='accelerator_oom',\n        device_error=error")
    script = script.replace("sys.stderr.write('CPU complete')", '''
sys.stderr.write('CPU decoding failed: ' + error)
print(json.dumps(dict(stage='error', message=error, actual_device='cpu', error_code='decoder_incomplete',
                     device_error=error, decoding_attempts=[dict(precision='float32', completed=False)])))
sys.exit(1)
''')
    task = recovery_task(tmp_path, script)
    try:
        with pytest.raises(EngineFailure) as failure:
            task._run_engine()
        assert '已自动切换 CPU 重试但仍未完成' in str(failure.value)
        assert '可尝试 CPU' not in str(failure.value)
        assert '请手动切换到 medium' in str(failure.value)
        assert task.engine.model == 'small'
        from autoskyplay.transcription.diagnostics import save_failure
        diagnostic = save_failure(tmp_path, task.work, str(failure.value), failure.value.details)
        report = json.loads(diagnostic.read_text(encoding='utf-8'))
        assert report['actual_device'] == 'cpu' and report['device_fallback']['from_device'] == 'mps'
        assert report['requested_device'] == 'auto'
        assert len(report['decoding_attempts']) == 1
        assert 'CPU decoding failed' in (diagnostic.parent/'cpu-retry.stderr').read_text()
        assert 'MPS decoding failed' in (diagnostic.parent/'engine.stderr').read_text()
        assert not list(task.work.glob('.cpu-retry-*'))
        assert sum(e.get('stage') == 'device' for e in task.events.queue) == 1
    finally:
        task.close()
