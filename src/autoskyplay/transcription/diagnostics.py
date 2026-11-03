"""Small durable failure reports, independent of disposable task audio."""

from __future__ import annotations

from pathlib import Path

from ..score import atomic_json
from .decoding import read_request


def save_failure(directory, work, message, details):
    destination = Path(directory) / 'transcription/diagnostics' / work.name
    destination.mkdir(parents=True, exist_ok=True)
    report = dict(message=message, task=work.name,
                  **{k: details[k] for k in ('engine_stage', 'audio_source', 'actual_device', 'error_code',
                                            'decoder_failure', 'device_error', 'decoding_attempts',
                                            'device_fallback', 'requested_device') if k in details})
    request = work / 'request.json'
    if request.is_file():
        data = read_request(work)
        report['selection'] = {k: data[k] for k in ('start', 'length') if k in data}
    for source in list(work.rglob('*.stderr'))[:20]:
        target = destination / source.relative_to(work)
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as stream:
            stream.seek(max(0, source.stat().st_size - 65536))
            target.write_bytes(stream.read(65536))
    atomic_json(destination / 'failure.json', report)
    return destination / 'failure.json'
