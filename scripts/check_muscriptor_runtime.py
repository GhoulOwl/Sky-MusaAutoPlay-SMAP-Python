"""Exercise the installed runtime through the desktop task, cache and cancel path."""
import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace

from autoskyplay.netease import NeteaseAudio, NeteaseSong
from autoskyplay.transcription.engine import select_engine
from autoskyplay.transcription.task import TranscriptionTask


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    def task(start=30, length=3):
        return TranscriptionTask(args.data, SimpleNamespace(), NeteaseSong('smoke','Runtime check','','',''),
            NeteaseAudio('smoke','',270810), select_engine(args.data,'small','cpu'), start, length,
            source=args.source, source_kind='local_audio')

    rows = []
    for iteration in range(2):
        t = task()
        try:
            before = time.perf_counter()
            t.start()
            t.thread.join(120)
            if t.running:
                t.cancel()
                t.thread.join(10)
            events = list(t.events.queue)
            assert any(e['stage'] == 'ready' for e in events), events
            rows.append(dict(iteration=iteration, seconds=time.perf_counter()-before,
                             notes=len(t.result.raw_notes), cache_hit=t.result.metadata['cache_hit'],
                             engine=t.engine.command[0]))
        finally:
            t.close()
        assert not t.work.exists()
    assert rows[-1]['cache_hit']
    t = task(31,200)
    try:
        t.start()
        deadline = time.monotonic()+30
        while t.running and time.monotonic() < deadline:
            if any(e['stage'] == 'pitch' for e in list(t.events.queue)):
                break
            time.sleep(.05)
        assert t.process and t.process.poll() is None
        t.cancel()
        t.thread.join(10)
        assert not t.running and t.process.poll() is not None and t.result is None
    finally:
        t.close()
    assert not t.work.exists()
    args.output.write_text(json.dumps(dict(runs=rows, cancellation=True, cleanup=True), indent=2))
    print(args.output)


if __name__ == '__main__':
    main()
