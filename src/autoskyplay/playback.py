"""Monotonic playback scheduler; UI and output are independent from score parsing."""

from __future__ import annotations

import bisect
import random
import threading
import time
from enum import Enum

from .score import Score


class State(str, Enum):
    STOPPED = "stopped"
    PLAYING = "playing"
    PAUSED = "paused"
    FINISHED = "finished"
    ERROR = "error"


class Timeline:
    """Pure song-clock operations, shared by the worker and deterministic tests."""

    def __init__(self, score: Score):
        self.notes = sorted(score.notes, key=lambda n: (n.time_ms, n.key))
        self.times = [n.time_ms for n in self.notes]
        self.duration_ms = score.duration_ms
        self.position_ms = 0.0
        self.index = 0

    def seek(self, position_ms: float):
        self.position_ms = min(self.duration_ms, max(0, position_ms))
        self.index = bisect.bisect_left(self.times, self.position_ms)

    def advance(self, delta_ms: float, speed: float):
        self.position_ms = min(self.duration_ms, self.position_ms + max(0, delta_ms) * speed)
        due = []
        while self.index < len(self.notes) and self.notes[self.index].time_ms <= self.position_ms:
            due.append(self.notes[self.index])
            self.index += 1
        # Group only equal timestamps; neighboring notes keep their separate attacks.
        groups = []
        for note in due:
            if not groups or groups[-1][0].time_ms != note.time_ms:
                groups.append([])
            groups[-1].append(note)
        return groups


class Player:
    def __init__(self, on_notes=lambda keys: None, on_done=lambda: None, on_error=lambda e: None):
        self.on_notes, self.on_done, self.on_error = on_notes, on_done, on_error
        self.state = State.STOPPED
        self.timeline: Timeline | None = None
        self.speed = 1.0
        self.random_speed = False
        self.countdown = 0.0
        self._condition = threading.Condition(threading.RLock())
        self._thread: threading.Thread | None = None
        self._cancel = False
        self._output = None
        self._silence_requested = False

    @property
    def position_ms(self):
        with self._condition:
            return self.timeline.position_ms if self.timeline else 0

    def load(self, score: Score):
        self.stop()
        with self._condition:
            self.timeline = Timeline(score)
            self.state = State.STOPPED

    def play(self, output, countdown: float = 0):
        if self.timeline is None or not self.timeline.notes:
            raise ValueError("当前曲目没有音符")
        if self.state == State.PAUSED and self._thread and self._thread.is_alive():
            self.pause(False)
            return
        self.stop()
        with self._condition:
            if self.timeline.position_ms >= self.timeline.duration_ms:
                self.timeline.seek(0)
            self._cancel = False
            self._output = output
            self.countdown = max(0, countdown)
            self.state = State.PLAYING
            self._thread = threading.Thread(target=self._run, name="score-playback", daemon=True)
            self._thread.start()

    def pause(self, paused: bool = True):
        with self._condition:
            if self.state not in (State.PLAYING, State.PAUSED):
                return
            self.state = State.PAUSED if paused else State.PLAYING
            self._silence_requested = paused
            self._condition.notify_all()

    def seek(self, position_ms: float):
        with self._condition:
            if self.timeline:
                self.timeline.seek(position_ms)
            self._silence_requested = True
            self._condition.notify_all()

    def set_speed(self, speed: float):
        with self._condition:
            self.speed = max(0.1, min(3, speed))

    def stop(self, rewind: bool = False):
        with self._condition:
            self._cancel = True
            self._condition.notify_all()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join()  # Never replace a still-running output worker.
        with self._condition:
            self._thread = None
            self.state = State.STOPPED
            self.countdown = 0
            if rewind and self.timeline:
                self.timeline.seek(0)

    def _run(self):
        last = time.monotonic()
        done = False
        random_remaining = random.randint(2, 5)
        error = None
        try:
            while True:
                with self._condition:
                    now = time.monotonic()
                    delta = max(0, (now - last) * 1000)
                    last = now
                    if self._cancel:
                        break
                    if self._silence_requested:
                        self._output.silence()
                        self._silence_requested = False
                        delta = 0
                    if self.state == State.PLAYING:
                        if self.countdown > 0:
                            self.countdown = max(0, self.countdown - delta / 1000)
                            delta = 0
                        else:
                            groups = self.timeline.advance(delta, self.speed)
                            for group in groups:
                                keys = tuple(n.key for n in group)
                                self._output.trigger(keys, tuple(n.duration_ms / self.speed for n in group))
                                self.on_notes(keys)
                                random_remaining -= 1
                                if self.random_speed and random_remaining <= 0:
                                    self.speed = random.uniform(0.5, 1.3)
                                    random_remaining = random.randint(2, 5)
                            if self.timeline.position_ms >= self.timeline.duration_ms:
                                done = True
                                break
                    self._output.pump()
                    self._condition.wait(0.002 if self.state == State.PLAYING else 0.02)
        except Exception as exc:
            error = str(exc)
        finally:
            try:
                if done and hasattr(self._output, "finish"):
                    self._output.finish()
                else:
                    self._output.silence()
            except Exception as exc:
                error = error or str(exc)
            with self._condition:
                self.state = State.ERROR if error else State.FINISHED if done else State.STOPPED
            if error:
                self.on_error(error)
            elif done:
                self.on_done()
