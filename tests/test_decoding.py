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
    report = json.loads(diagnostic.read_text())
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
