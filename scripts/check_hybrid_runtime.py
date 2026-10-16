"""Real installed-component cache, isolation, cancellation and cleanup checks."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from autoskyplay.netease import NeteaseAudio, NeteaseSong  # noqa: E402
from autoskyplay.transcription.engine import select_hybrid_engine  # noqa: E402
from autoskyplay.transcription.task import TranscriptionTask  # noqa: E402


def main():
    import psutil

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []

    def make(start=91.234567, length=12, background="with_drums", cancel_stage=None):
        engine = select_hybrid_engine(args.data, "small", "auto", background)
        if cancel_stage == "game":
            engine = replace(engine, config=dict(engine.config, game={"steps": 9}))
        if cancel_stage == "background":
            engine = replace(engine, model="medium")
        return TranscriptionTask(
            args.cache,
            SimpleNamespace(),
            NeteaseSong("probe", "Probe", "", "", ""),
            NeteaseAudio("probe", "", 270810),
            engine,
            start,
            length,
            source=args.source,
            source_kind="local_audio",
        )

    def exercise(task, cancel_stage=None):
        started = time.perf_counter()
        pids = set()
        max_rss = 0
        canceled = False
        task.start()
        try:
            deadline = time.monotonic() + 300
            while task.running and time.monotonic() < deadline:
                p = task.process
                if p and p.poll() is None:
                    pids.add(p.pid)
                    try:
                        ps = psutil.Process(p.pid)
                        max_rss = max(
                            max_rss,
                            sum(
                                q.memory_info().rss
                                for q in [ps, *ps.children(recursive=True)]
                                if q.is_running()
                            ),
                        )
                    except psutil.Error:
                        pass
                    if (
                        cancel_stage
                        and "--stage" in p.args
                        and p.args[p.args.index("--stage") + 1] == cancel_stage
                    ):
                        seen = list(task.events.queue)
                        active = (
                            any(e["stage"] == "separate" and e.get("progress", 0) > 0 for e in seen)
                            if cancel_stage == "separate"
                            else any(
                                e["stage"] == "load"
                                and ("GAME" if cancel_stage == "game" else "MuScriptor")
                                in e.get("message", "")
                                for e in seen
                            )
                        )
                        if active:
                            task.cancel()
                            canceled = True
                time.sleep(0.05)
            if task.running:
                task.cancel()
                task.thread.join(10)
                raise AssertionError("hybrid runtime timeout")
            events = list(task.events.queue)
            if cancel_stage:
                assert canceled and task.result is None and any(e["stage"] == "cancelled" for e in events), (
                    events
                )
                assert not any(e["stage"] == "ready" for e in events)
                row = dict(cancel_stage=cancel_stage, cancelled=True)
            else:
                assert any(e["stage"] == "ready" for e in events), events
                row = dict(
                    cache_hits=task.result.metadata["cache_hits"],
                    profiles=task.result.metadata["stage_profiles"],
                    stage_seconds=task.result.metadata["timings_seconds"],
                    notes=len(task.result.raw_notes),
                )
            row.update(
                seconds=time.perf_counter() - started, peak_child_tree_rss_bytes=max_rss, pids=sorted(pids)
            )
            rows.append(row)
        finally:
            task.close()
        assert not task.work.exists()
        assert not any(psutil.pid_exists(pid) for pid in pids)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(dict(rows=rows, cleanup=True), indent=2))
        print(json.dumps({k: v for k, v in rows[-1].items() if k != "profiles"}), flush=True)

    exercise(make())
    exercise(make())
    assert all(rows[-1]["cache_hits"].values())
    exercise(make(background="without_drums"))
    assert all(rows[-1]["cache_hits"][s] for s in ("separation", "features", "game"))
    assert not rows[-1]["cache_hits"]["background"]
    for stage in ("separate", "game", "background"):
        exercise(
            make(start=92.345678, length=35, cancel_stage=stage)
            if stage == "separate"
            else make(cancel_stage=stage),
            stage,
        )


if __name__ == "__main__":
    main()
