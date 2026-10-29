"""Strict EOS with bounded retries on the same model and complete input."""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

DECODING_POLICY = 'pitch-only-strict-eos-fp32-beam2-1'
PITCHED_INPUTS = ('bass', 'guitar', 'piano', 'other')
EOS_FAILURE = re.compile(r'chunk (\d+) \(seek=([0-9.]+)s\) did not emit EOS within (\d+) tokens')
SOURCE_NAMES = dict(original='原混音', background='混合背景', background_no_drums='无鼓背景',
                    bass='贝斯轨', guitar='吉他轨', piano='钢琴轨', other='其他轨', lead_vocals='主唱轨')


def pitched_model_class(base):
    """Use the pinned runtime's native mask, excluding only drum token IDs.

    All pitched program labels, including miscellaneous groups, remain legal.
    Passing all named instruments to transcribe() would also change conditioning
    and exclude unnamed program groups, so it is intentionally avoided here.
    """
    class PitchedModel(base):
        def _generate_token_stream(self, all_conditions, seek_times, batch_size, max_gen_len, use_sampling,
                                   temperature, cfg_coef, no_eos_is_ok, prelude_forcing=True,
                                   beam_size=1, forbidden_tokens=None):
            import torch
            blocked = torch.tensor([i for i, event in enumerate(self._tokenizer._vocab) if event.type == 'drum'],
                                   device=self._device, dtype=torch.long)
            if forbidden_tokens is not None:
                blocked = torch.cat((forbidden_tokens, blocked)).unique()
            return super()._generate_token_stream(all_conditions, seek_times, batch_size, max_gen_len, use_sampling,
                                                  temperature, cfg_coef, no_eos_is_ok, prelude_forcing,
                                                  beam_size, blocked)

    return PitchedModel


def eos_failure(exc):
    match = EOS_FAILURE.search(str(exc))
    if not match:
        return None
    try:
        data = dict(chunk=int(match[1]), seek_seconds=float(match[2]), token_limit=int(match[3]))
        return data if math.isfinite(data['seek_seconds']) else None
    except ValueError:
        return None


def read_request(work):
    try:
        data = json.loads((Path(work) / 'request.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def strict_decode(load, consume, release, *, device, notice=lambda message: None):
    """Retry complete inputs on EOS failure, discarding all failed events.

    Callbacks keep torch out of the desktop process and make the retry boundary
    explicit. Consume must always use the same complete input and strict EOS.
    First retry MPS FP16 in FP32, then try native beam_size=2. No model changes,
    sampling, token-limit increases or audio cuts. All other errors propagate.
    """
    attempts = []
    plans = [('float16', 1), ('float32', 1), ('float32', 2)] if device == 'mps' else [
        ('float32', 1), ('float32', 2)]
    for index, (precision, beam_size) in enumerate(plans):
        began = time.perf_counter()
        model = load(precision)
        loaded = time.perf_counter()
        record = dict(precision=precision, beam_size=beam_size, device=device, load_seconds=loaded - began)
        retry = False
        try:
            outcome = consume(model, beam_size=beam_size)
            record['completed'] = True
        except RuntimeError as exc:
            record.update(completed=False, error=str(exc)[:500], decoder_failure=eos_failure(exc))
            if index == len(plans) - 1 or record['decoder_failure'] is None:
                exc.decoding_attempts = [*attempts, record]
                raise
            retry = True
        finally:
            record['inference_seconds'] = time.perf_counter() - loaded
            attempts.append(record)
            release(model)
            del model
        if not retry:
            return outcome, attempts
        notice('MPS 半精度识别未正常结束，将使用同一模型的 FP32 从头重试当前完整音轨；已丢弃本次不完整事件。'
               if precision == 'float16' else
               '贪心解码未正常结束，将使用同一模型的双候选解码从头重试完整音轨；已丢弃本次不完整事件。')
    raise AssertionError('precision retry exhausted without success or exception')


def worker_failure_message(exc, request=None):
    failure = eos_failure(exc)
    attempts = dict(decoding_attempts=exc.decoding_attempts) if hasattr(exc, 'decoding_attempts') else {}
    if failure is None:
        return None, attempts
    request = request or {}
    source = request.get('audio_source', 'original')
    if not isinstance(source, str):
        source = 'original'
    try:
        origin = float(request.get('input_origin_seconds', request.get('start', 0)))
        origin = origin if math.isfinite(origin) else 0.
    except (TypeError, ValueError):
        origin = 0.
    position = origin + failure['seek_seconds']
    clock = f'{int(position // 60):02d}:{position % 60:04.1f}'
    label = SOURCE_NAMES.get(source, '器乐轨')
    message = (f'{label}在原曲约 {clock} 处未正常结束解码（达到 {failure["token_limit"]} token）；'
               '本次未保存不完整曲谱，可尝试 CPU 或 medium 模型。')
    return message, dict(attempts, error_code='decoder_incomplete', decoder_failure=failure, audio_source=source)
