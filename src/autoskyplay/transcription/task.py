"""One cancellable transcription task, with a credential-free local worker."""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ..score import atomic_json
from .melody import PitchResult

STAGES = {"decode", "separate", "pitch", "convert", "notice", "done", "error"}


def stop_process(process):
    if process and process.poll() is None:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)


class TranscriptionTask:
    def __init__(self, directory, client, song, audio, engine, start, length, source=None):
        origin = audio.trial_start or 0
        available = audio.available_seconds
        if (not 0 <= start - origin or not 0 < length <= 600
                or (available and start - origin + length > available + .1)):
            raise ValueError("所选范围超出可用音频，或超过 10 分钟")
        self.client, self.song, self.audio, self.engine = client, song, audio, engine
        self.source = Path(source) if source else None
        self.start_seconds, self.length_seconds = start, length
        self.cancelled = threading.Event()
        self.disposed = threading.Event()
        self.completed = threading.Event()
        self.cleanup_lock = threading.Lock()
        self.events = queue.Queue()
        self.process = None
        self.result = None
        base = (Path(directory) / "transcription" / "tasks").resolve()
        base.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix="task-", dir=base))
        self.thread = threading.Thread(target=self._run, name="local-transcription", daemon=True)

    @property
    def running(self):
        return self.thread.is_alive()

    def start(self):
        self.thread.start()

    def cancel(self):
        self.cancelled.set()

    def close(self):
        self.cancel()
        with self.cleanup_lock:
            self.disposed.set()
            if self.completed.is_set() or self.thread.ident is None:
                shutil.rmtree(self.work, ignore_errors=True)

    def emit(self, stage, progress=0, **values):
        self.events.put(dict(stage=stage, progress=progress, **values))

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise ValueError("扒谱已取消")

    def _run(self):
        try:
            self.check_cancelled()
            self.emit("download")
            # Refresh an expiring URL once. Never change full/trial scope silently.
            if self.source and self.source.is_file():
                shutil.copyfile(self.source, self.work / "source.audio")
            for attempt in range(0 if (self.work / "source.audio").exists() else 2):
                try:
                    self.client.download_audio(self.audio, self.work / "source.audio",
                                               self.cancelled.is_set, lambda p: self.emit("download", p))
                    break
                except ValueError:
                    self.check_cancelled()
                    if attempt:
                        raise
                    updated = self.client.playback_audio(self.song.id)
                    if (updated.trial_start, updated.trial_end) != (self.audio.trial_start, self.audio.trial_end):
                        raise ValueError("歌曲可用范围已变化，请重新获取歌曲") from None
                    self.audio = updated
            self.check_cancelled()
            cover = None
            try:
                cover = self.client.cover(self.song)
            except ValueError:
                self.emit("notice", message="封面获取失败，生成曲谱后仍可手动设置封面")
            self.check_cancelled()
            atomic_json(self.work / "request.json", {
                "start": self.start_seconds - (self.audio.trial_start or 0), "length": self.length_seconds,
            })
            metadata = self._run_engine()
            self.check_cancelled()
            metadata.update(source="netease", song_id=self.song.id, source_start=self.start_seconds,
                            requested_length=self.length_seconds, is_trial=self.audio.is_trial)
            self.result = PitchResult.load(self.work / "pitch.npz", metadata)
            self.emit("ready", 100, result=self.result, cover=cover)
        except Exception as exc:
            self.emit("cancelled" if self.cancelled.is_set() else "error",
                      message="扒谱已取消" if self.cancelled.is_set() else str(exc))
        finally:
            stop_process(self.process)
            with self.cleanup_lock:
                self.completed.set()
                if self.disposed.is_set():
                    shutil.rmtree(self.work, ignore_errors=True)
            self.emit("finished")

    def _run_engine(self):
        command = [*self.engine.command, "--work", str(self.work), "--models", str(self.engine.models),
                   "--parent", str(os.getpid())]
        env = os.environ.copy()
        # The worker is fully offline and does not inherit NetEase credentials.
        for name in list(env):
            if (name.startswith(("AUTOSKYPLAY_NETEASE", "NETEASE_COOKIE", "MUSIC_U"))
                    or name in ("PYTHONPATH", "PYTHONHOME", "IMAGEIO_FFMPEG_EXE", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES")):
                env.pop(name, None)
        env.update(HF_HUB_OFFLINE="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        lines = queue.Queue(maxsize=100)
        with (self.work / "engine.stderr").open("wb") as errors:
            self.process = subprocess.Popen(
                command, cwd=self.work, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=errors, start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

            def read_lines():
                try:
                    while line := self.process.stdout.readline(8192):
                        while True:
                            if self.cancelled.is_set():
                                return
                            try:
                                lines.put(line, timeout=.1)
                                break
                            except queue.Full:
                                pass
                finally:
                    self.process.stdout.close()
                    while not self.cancelled.is_set():
                        try:
                            lines.put(None, timeout=.1)
                            break
                        except queue.Full:
                            pass

            reader = threading.Thread(target=read_lines, daemon=True)
            reader.start()
            deadline = time.monotonic() + 1800
            done, error, eof = None, None, False
            try:
                while not eof:
                    self.check_cancelled()
                    if time.monotonic() > deadline:
                        raise ValueError("扒谱超过 30 分钟，请选择更短的片段")
                    try:
                        line = lines.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if line is None:
                        eof = True
                        continue
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict) or event.get("stage") not in STAGES:
                            continue
                        if event["stage"] == "done":
                            done = {k: v for k, v in event.items() if k not in ("stage", "progress")}
                        elif event["stage"] == "error":
                            error = str(event.get("message", "本地引擎运行失败"))[:500]
                        else:
                            self.events.put(event)
                    except (ValueError, TypeError):
                        continue
                code = self.process.wait(timeout=5)
                if error or code or not done:
                    raise ValueError(error or "本地扒谱引擎异常退出，请重新安装组件或选择更短片段")
                return done
            finally:
                stop_process(self.process)
                reader.join(timeout=1)
