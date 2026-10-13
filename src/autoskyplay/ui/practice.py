from __future__ import annotations

import bisect
import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QSlider, QVBoxLayout

from ..playback import Player, State
from ..practice import Practice
from . import style
from .widgets import Piano, ScoreWall, button, label


class PracticeDialog(QDialog):
    fired = Signal(object)
    done = Signal()
    error = Signal(str)

    def __init__(self, host, score):
        super().__init__(host)
        self.host, self.score = host, score
        self.practice = Practice(score)
        self.demo_player = Player(self.fired.emit, self.done.emit, self.error.emit)
        self.demo_player.load(score)
        self.fired.connect(self.demonstrated)
        self.done.connect(self.demonstration_done)
        self.error.connect(host.show_error)
        self.resize(1120, 820)
        self.setWindowTitle(style.tr("practice") + " · " + score.name)
        root = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(button("back", self.close))
        self.title_label = label(score.name, "heading")
        row.addWidget(self.title_label)
        row.addStretch()
        self.game = QCheckBox(style.tr("gamepractice"))
        self.game.setEnabled(sys.platform == "win32")
        self.game.toggled.connect(self.game_mode)
        row.addWidget(self.game)
        root.addLayout(row)
        self.progress = label("", "muted")
        root.addWidget(self.progress)
        self.wall = ScoreWall(self.practice)
        root.addWidget(self.wall, 1)
        self.piano = Piano(host.store.data["keys"])
        self.piano.setMinimumHeight(320)
        self.piano.pressed.connect(self.press)
        self.piano.released.connect(self.release)
        root.addWidget(self.piano, 2)
        controls = QHBoxLayout()
        controls.addWidget(button("reset", self.reset))
        self.demo_button = button("demonstrate", self.demonstrate, accent=True)
        controls.addWidget(self.demo_button)
        controls.addWidget(button("pause", self.pause))
        self.beat = QCheckBox(style.tr("beatmode"))
        self.beat.toggled.connect(self.beat_mode)
        controls.addWidget(self.beat)
        self.speed = host.speed.value()
        self.speed_label = label(f"{self.speed:.1f}×")
        controls.addWidget(self.speed_label)
        controls.addStretch()
        root.addLayout(controls)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, len(self.practice.groups))
        self.slider.sliderMoved.connect(self.seek)
        root.addWidget(self.slider)
        self.beat_timer = QTimer(self)
        self.beat_timer.timeout.connect(self.beat_step)
        self.update_hints()

    def press(self, key):
        self.piano.keys[key].down = True
        self.piano.flash((key,))
        # Game practice listens without sending additional synthetic keystrokes.
        if not self.game.isChecked() and self.host.ensure_audio():
            self.host.mixer.note_on(key)
        if self.demo_player.state not in (State.PLAYING, State.PAUSED):
            self.practice.press(key)
            self.update_hints()

    def release(self, key):
        self.piano.keys[key].down = False
        self.piano.keys[key].update()
        self.practice.release(key)
        self.host.mixer.note_off(key)

    def update_hints(self):
        self.piano.hints(self.practice.expected(), self.practice.expected(1))
        self.wall.update()
        self.progress.setText(
            style.tr("completed")
            if self.practice.finished
            else f"{self.practice.index + 1} / {len(self.practice.groups)} · {self.score.bpm:g} BPM"
        )
        if not self.slider.isSliderDown():
            self.slider.setValue(self.practice.index)

    def seek(self, index):
        self.piano.release_all()
        self.practice.seek(index)
        if index < len(self.practice.groups):
            self.demo_player.seek(self.practice.groups[index][0])
        else:
            self.demo_player.stop()
        self.update_hints()

    def reset(self):
        self.demo_player.stop(True)
        self.seek(0)

    def demonstrate(self):
        self.beat.setChecked(False)
        if self.demo_player.state == State.PLAYING:
            self.demo_player.stop()
            self.update_hints()
            return
        if self.host.ensure_audio():
            try:
                self.demo_player.set_speed(self.speed)
                if self.practice.finished:
                    self.seek(0)
                position = self.practice.groups[self.practice.index][0] if self.practice.groups else 0
                self.demo_player.seek(position)
                self.demo_player.play(self.host.mixer)  # Always speaker output, including game practice.
            except ValueError as exc:
                self.host.show_error(str(exc))

    def demonstrated(self, keys):
        times = [g[0] for g in self.practice.groups]
        index = bisect.bisect_right(times, self.demo_player.position_ms) - 1
        self.practice.index = min(len(times), max(0, index + 1))
        self.piano.flash(keys)
        self.update_hints()

    def demonstration_done(self):
        self.practice.seek(len(self.practice.groups))
        self.update_hints()

    def pause(self):
        if self.beat.isChecked():
            self.beat_timer.stop() if self.beat_timer.isActive() else self.beat_timer.start()
            self.host.mixer.metronome_bpm = self.score.bpm * self.speed if self.beat_timer.isActive() else 0
        else:
            self.demo_player.pause(self.demo_player.state != State.PAUSED)

    def beat_mode(self, enabled):
        self.demo_player.stop()
        if enabled and self.host.ensure_audio():
            self.beat_timer.start(max(20, int(self.score.ms_per_beat / 4 / self.speed)))
            self.host.mixer.metro_frame = 0
            self.host.mixer.metronome_bpm = self.score.bpm * self.speed
        else:
            self.beat_timer.stop()
            self.host.mixer.metronome_bpm = 0

    def beat_step(self):
        self.practice.seek(self.practice.index + 1)
        self.update_hints()
        if self.practice.finished:
            self.beat.setChecked(False)

    def game_mode(self, enabled):
        if enabled:
            self.host.start_global_keyboard(force=True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
        self.show()

    def hotkey(self, key):
        if key == "F1":
            self.reset()
        elif key == "F2":
            self.pause()
        elif key in ("F3", "F4"):
            self.speed = max(0.1, min(3, self.speed + (-0.1 if key == "F3" else 0.1)))
            self.demo_player.set_speed(self.speed)
            self.speed_label.setText(f"{self.speed:.1f}×")
            if self.beat.isChecked():
                self.beat_mode(True)
        elif key in ("LEFT", "RIGHT"):
            self.seek(self.practice.index + (-1 if key == "LEFT" else 1))
        elif key in ("F5", "F6"):
            score = self.host.adjacent_score(-1 if key == "F5" else 1)
            if score:
                self.demo_player.stop()
                self.score = score
                self.title_label.setText(score.name)
                self.setWindowTitle(style.tr("practice") + " · " + score.name)
                self.practice = Practice(score)
                self.wall.practice = self.practice
                self.demo_player.load(score)
                self.slider.setRange(0, len(self.practice.groups))
                self.update_hints()
        elif key == "ESC":
            self.close()

    def closeEvent(self, event):
        self.demo_player.stop()
        self.beat_timer.stop()
        self.host.mixer.metronome_bpm = 0
        self.piano.release_all()
        self.host.mixer.silence()
        if not self.host.store.data["global_hotkeys"]:
            self.host.global_keyboard.stop()
        event.accept()
