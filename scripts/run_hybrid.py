"""Run the hybrid engine locally, with the same task/cancellation/cache as the UI."""

from __future__ import annotations

import argparse
import json
import queue
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

from autoskyplay.netease import NeteaseAudio, NeteaseSong
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.engine import select_hybrid_engine
from autoskyplay.transcription.task import TranscriptionTask


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--length", type=float, required=True)
    parser.add_argument("--model", choices=["small", "medium"], default="small")
    parser.add_argument("--device", choices=["auto", "cpu", "mps", "cuda"], default="auto")
    parser.add_argument("--background", choices=["with_drums", "without_drums"], default="with_drums")
    parser.add_argument("--game-device", choices=["auto", "cpu", "mps", "cuda"],
                        help='Override GAME only; otherwise inherit --device')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "complete.json").unlink(missing_ok=True)
    engine = select_hybrid_engine(args.data, args.model, args.device, args.background)
    if args.game_device:
        engine.config["game_device"] = args.game_device
    task = TranscriptionTask(
        args.cache or args.data,
        SimpleNamespace(),
        NeteaseSong("local", args.audio.stem, "", "", ""),
        NeteaseAudio("local", "", round((args.start + args.length) * 1000)),
        engine,
        args.start,
        args.length,
        source=args.audio,
        source_kind="local",
        source_name=args.audio.name,
    )
    beginning = time.perf_counter()
    ready = False
    task.start()
    try:
        with (args.output / "progress.jsonl").open("w") as log:
            while True:
                try:
                    event = task.events.get(timeout=0.2)
                except queue.Empty:
                    continue
                safe = {k: v for k, v in event.items() if k not in ("result", "cover")}
                safe["elapsed_seconds"] = time.perf_counter() - beginning
                line = json.dumps(safe, ensure_ascii=False)
                log.write(line + "\n")
                log.flush()
                print(line, flush=True)
                if event["stage"] == "ready":
                    ready = True
                if event["stage"] == "finished":
                    break
        task.thread.join()
        for file in task.work.iterdir():
            destination = args.output / file.name
            if file.is_dir():
                shutil.copytree(file, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(file, destination)
        if task.result is None or not ready:
            raise SystemExit("混合任务失败，见 progress.jsonl 和 engine.stderr")
        for instrument, mode in (("Piano", "auto"), ("Violin", "auto"), ("Violin", "simple")):
            score, _ = build_score(task.result, args.audio.stem, instrument=instrument, mode=mode)
            score.save(args.output / f"{instrument}-{mode}.json")
        task.result.export_midi(args.output / "raw.mid")
        from autoskyplay.transcription.models import digest

        (args.output / "complete.json").write_text(
            json.dumps(
                dict(
                    events_sha256=digest(args.output / "events.json"),
                    source_sha256=task.result.metadata["source_sha256"],
                    total_seconds=time.perf_counter() - beginning,
                )
            )
        )
    except KeyboardInterrupt:
        task.cancel()
        task.thread.join(timeout=10)
        raise
    finally:
        task.close()


if __name__ == "__main__":
    main()
