from __future__ import annotations

import base64
import copy
import math

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..playback import Player, State
from ..score import Note, Score, unique_path
from . import style
from .widgets import button, label


class NoteItem(QGraphicsRectItem):
    def __init__(self, note: Note, roll):
        self.note, self.roll = note, roll
        self.initial_pos = None
        self.snapping = False
        width = max(7, note.duration_ms / roll.score.ms_per_beat * roll.pixels_per_beat)
        super().__init__(0, 0, width, roll.row_height - 6)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setBrush(QColor(style.colors["accent"]))
        self.setPen(QPen(QColor(style.colors["ink"]), 1))
        self.setPos(
            note.time_ms / roll.score.ms_per_beat * roll.pixels_per_beat, note.key * roll.row_height + 3
        )
        self.snapping = True

    def itemChange(self, change, value):
        if self.snapping and change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            grid = self.roll.pixels_per_beat / self.roll.subdivision
            x = max(0, round(value.x() / grid) * grid)
            row = min(14, max(0, round((value.y() - 3) / self.roll.row_height)))
            return QPointF(x, row * self.roll.row_height + 3)
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        self.initial_pos = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        changed = False
        for item in self.roll.scene().selectedItems():
            if isinstance(item, NoteItem):
                time_ms = item.x() / self.roll.pixels_per_beat * self.roll.score.ms_per_beat
                key = int(round((item.y() - 3) / self.roll.row_height))
                if item.note.time_ms != time_ms or item.note.key != key:
                    changed = True
                item.note.time_ms, item.note.key = time_ms, key
        if changed:
            self.roll.changed.emit()


class PianoRoll(QGraphicsView):
    changed = Signal()

    def __init__(self, score: Score):
        super().__init__()
        self.score = score
        self.pixels_per_beat = 70
        self.row_height = 30
        self.subdivision = 4
        self.cursor_beat = 0.0
        self.total_beats = max(32, math.ceil(score.duration_ms / score.ms_per_beat) + 8)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setMinimumHeight(480)
        self.rebuild()

    def rebuild(self):
        self.scene().clear()
        self.total_beats = max(
            self.total_beats, math.ceil(self.score.duration_ms / self.score.ms_per_beat) + 8
        )
        self.scene().setSceneRect(0, 0, self.total_beats * self.pixels_per_beat, 15 * self.row_height)
        for note in self.score.notes:
            self.scene().addItem(NoteItem(note, self))
        self.viewport().update()

    def drawBackground(self, painter, rect):
        c = style.colors
        painter.fillRect(rect, QColor(c["panel"]))
        for row in range(15):
            if row % 2 == 0:
                painter.fillRect(
                    QRectF(rect.left(), row * self.row_height, rect.width(), self.row_height),
                    QColor(c["raised"]),
                )
            painter.setPen(QColor(c["border"]))
            painter.drawLine(
                QPointF(rect.left(), row * self.row_height), QPointF(rect.right(), row * self.row_height)
            )
        cell = self.pixels_per_beat / self.subdivision
        start = int(rect.left() / cell)
        stop = int(rect.right() / cell) + 1
        for index in range(start, stop):
            major = index % (4 * self.subdivision) == 0
            painter.setPen(QPen(QColor(c["muted"] if major else c["border"]), 1.2 if major else 0.5))
            painter.drawLine(QPointF(index * cell, 0), QPointF(index * cell, 15 * self.row_height))

    def drawForeground(self, painter, rect):
        painter.setPen(QPen(QColor(style.colors["accent"]), 2))
        x = self.cursor_beat * self.pixels_per_beat
        painter.drawLine(QPointF(x, 0), QPointF(x, 15 * self.row_height))

    def add_note(self, key: int, beat: float | None = None):
        beat = self.cursor_beat if beat is None else beat
        time_ms = beat * self.score.ms_per_beat
        if not any(n.key == key and abs(n.time_ms - time_ms) < 0.1 for n in self.score.notes):
            self.score.notes.append(Note(key, time_ms, self.score.ms_per_beat / self.subdivision))
            self.rebuild()
            self.changed.emit()

    def mousePressEvent(self, event):
        position = self.mapToScene(event.position().toPoint())
        item = self.itemAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.RightButton and isinstance(item, NoteItem):
            self.score.notes.remove(item.note)
            self.rebuild()
            self.changed.emit()
            return
        if event.button() == Qt.MouseButton.LeftButton and item is None and not event.modifiers():
            key = int(position.y() // self.row_height)
            beat = max(0, round(position.x() / self.pixels_per_beat * self.subdivision) / self.subdivision)
            if 0 <= key < 15:
                self.cursor_beat = beat
                self.add_note(key, beat)
            return
        super().mousePressEvent(event)

    def delete_selected(self):
        notes = [item.note for item in self.scene().selectedItems() if isinstance(item, NoteItem)]
        if notes:
            self.score.notes = [n for n in self.score.notes if not any(n is old for old in notes)]
            self.rebuild()
            self.changed.emit()


class EditorDialog(QDialog):
    note_fired = Signal(object)
    error = Signal(str)

    def __init__(self, parent, score: Score):
        super().__init__(parent)
        self.host = parent
        self.score = copy.deepcopy(score)
        self.saved = False
        self.dirty = False
        self.history = [copy.deepcopy(self.score.notes)]
        self.history_index = 0
        self.player = Player(self.note_fired.emit, on_error=self.error.emit)
        self.note_fired.connect(self.on_preview_notes)
        self.error.connect(parent.show_error)
        self.resize(1050, 720)
        self.setWindowTitle(style.tr("edit"))
        root = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        toolbar.addWidget(label(style.tr("edit"), "heading"))
        toolbar.addStretch()
        toolbar.addWidget(button("undo", self.undo))
        toolbar.addWidget(button("redo", self.redo))
        toolbar.addWidget(button("preview", self.preview))
        toolbar.addWidget(button("stop", self.player.stop))
        toolbar.addWidget(button("saveas", lambda: self.save(True)))
        toolbar.addWidget(button("save", self.save, accent=True))
        root.addLayout(toolbar)
        metadata = QHBoxLayout()
        self.name = QLineEdit(self.score.name)
        self.author = QLineEdit(self.score.author)
        self.transcriber = QLineEdit(self.score.transcriber)
        for key, widget in [("title", self.name), ("artist", self.author), ("transcriber", self.transcriber)]:
            form = QFormLayout()
            form.addRow(style.tr(key), widget)
            metadata.addLayout(form)
            widget.textEdited.connect(self.mark_dirty)
        self.bpm = QDoubleSpinBox()
        self.bpm.setRange(1, 2000)
        self.bpm.setDecimals(2)
        self.bpm.setValue(self.score.bpm)
        self.bpm.valueChanged.connect(self.change_bpm)
        form = QFormLayout()
        form.addRow("BPM", self.bpm)
        metadata.addLayout(form)
        root.addLayout(metadata)
        controls = QHBoxLayout()
        self.grid = QComboBox()
        for text, value in [
            ("1/4", 1),
            ("1/8", 2),
            ("1/16", 4),
            ("1/32", 8),
            ("Triplet 1/8", 3),
            ("Triplet 1/16", 6),
        ]:
            self.grid.addItem(text, value)
        self.grid.setCurrentIndex(2)
        self.grid.currentIndexChanged.connect(self.change_grid)
        controls.addWidget(label(style.tr("grid")))
        controls.addWidget(self.grid)
        self.length = QSpinBox()
        self.length.setRange(8, 10000)
        self.roll = PianoRoll(self.score)
        self.length.setValue(self.roll.total_beats)
        self.length.valueChanged.connect(self.change_length)
        controls.addWidget(label(style.tr("beats")))
        controls.addWidget(self.length)
        controls.addWidget(button("delete", self.roll.delete_selected))
        controls.addWidget(button("cover", self.set_cover))
        self.note_length = QDoubleSpinBox()
        self.note_length.setRange(0.03125, 16)
        self.note_length.setValue(0.25)
        self.note_length.setSingleStep(0.25)
        controls.addWidget(label(style.tr("notelength")))
        controls.addWidget(self.note_length)
        controls.addWidget(button("apply", self.apply_note_length))
        controls.addStretch()
        root.addLayout(controls)
        roll_row = QHBoxLayout()
        bindings = QWidget()
        bindings.setFixedWidth(40)
        rows = QVBoxLayout(bindings)
        rows.setContentsMargins(0, 2, 0, 0)
        rows.setSpacing(0)
        for key in self.host.store.data["keys"]:
            row_label = QLabel(key)
            row_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row_label.setFixedHeight(self.roll.row_height)
            rows.addWidget(row_label)
        rows.addStretch()
        roll_row.addWidget(bindings)
        roll_row.addWidget(self.roll, 1)
        root.addLayout(roll_row, 1)
        root.addWidget(
            label(
                "左键空格写入 · 拖动音符移动 · 右键删除 · Ctrl+拖动框选 · 15 键写谱 · ←/→ 移动光标 · Ctrl+S 保存",
                "muted",
            )
        )
        self.roll.changed.connect(self.record)
        for shortcut, callback in [
            ("Ctrl+S", self.save),
            ("Ctrl+Shift+S", lambda: self.save(True)),
            ("Ctrl+Z", self.undo),
            ("Ctrl+Shift+Z", self.redo),
            ("Ctrl+Y", self.redo),
            ("Delete", self.roll.delete_selected),
        ]:
            action = QShortcut(QKeySequence(shortcut), self)
            action.activated.connect(callback)

    def mark_dirty(self):
        self.dirty = True
        self.setWindowTitle(style.tr("edit") + " *")

    def record(self):
        self.player.stop()
        self.history = self.history[: self.history_index + 1]
        self.history.append(copy.deepcopy(self.score.notes))
        self.history_index += 1
        self.mark_dirty()

    def undo(self):
        if self.history_index > 0:
            self.player.stop()
            self.history_index -= 1
            self.score.notes = copy.deepcopy(self.history[self.history_index])
            self.roll.rebuild()
            self.mark_dirty()

    def redo(self):
        if self.history_index + 1 < len(self.history):
            self.player.stop()
            self.history_index += 1
            self.score.notes = copy.deepcopy(self.history[self.history_index])
            self.roll.rebuild()
            self.mark_dirty()

    def change_bpm(self, value):
        if hasattr(self, "roll"):
            self.player.stop()
            self.score.set_bpm(value)
            self.roll.rebuild()
            self.mark_dirty()

    def change_grid(self):
        self.roll.subdivision = self.grid.currentData()
        self.roll.viewport().update()

    def change_length(self, value):
        self.roll.total_beats = value
        self.roll.rebuild()

    def apply_note_length(self):
        items = self.roll.scene().selectedItems()
        if items:
            for item in items:
                if isinstance(item, NoteItem):
                    item.note.duration_ms = self.note_length.value() * self.score.ms_per_beat
            self.roll.rebuild()
            self.record()

    def set_cover(self):
        path, _ = QFileDialog.getOpenFileName(
            self, style.tr("cover"), "", "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if not path:
            return
        from PySide6.QtGui import QImage

        image = QImage(path)
        if image.isNull():
            self.host.show_error("图片读取失败")
            return
        image = image.scaled(
            512, 512, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "JPEG", 80)
        self.score.root["cover"] = base64.b64encode(bytes(data)).decode("ascii")
        self.mark_dirty()

    def save(self, save_as=False):
        self.score.name = self.name.text().strip() or "未命名"
        self.score.author = self.author.text().strip()
        self.score.transcriber = self.transcriber.text().strip()
        target = self.score.path
        if save_as or target is None:
            default = unique_path(self.host.store.songs_dir, self.score.name)
            path, _ = QFileDialog.getSaveFileName(
                self, style.tr("saveas"), str(default), "Sky score (*.json *.txt)"
            )
            if not path:
                return False
            target = path
        try:
            self.score.save(target)
        except (OSError, ValueError) as exc:
            self.host.show_error(str(exc))
            return False
        self.saved, self.dirty = True, False
        self.setWindowTitle(style.tr("edit"))
        return True

    def preview(self):
        if self.player.state == State.PLAYING:
            self.player.stop()
            return
        if self.host.ensure_audio():
            try:
                self.player.load(self.score)
                self.player.set_speed(self.host.player.speed)
                self.player.play(self.host.mixer)
            except ValueError as exc:
                self.host.show_error(str(exc))

    def on_preview_notes(self, keys):
        self.roll.cursor_beat = self.player.position_ms / self.score.ms_per_beat
        self.roll.viewport().update()
        self.roll.ensureVisible(self.roll.cursor_beat * self.roll.pixels_per_beat, 0, 20, 20)

    def keyPressEvent(self, event):
        if isinstance(self.focusWidget(), (QLineEdit, QSpinBox, QDoubleSpinBox)):
            super().keyPressEvent(event)
            return
        text = event.text().upper()
        bindings = self.host.store.data["keys"]
        if text in bindings and not event.isAutoRepeat():
            key = bindings.index(text)
            self.roll.add_note(key)
            if self.host.ensure_audio():
                self.host.mixer.trigger((key,))
            event.accept()
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            direction = -1 if event.key() == Qt.Key.Key_Left else 1
            self.roll.cursor_beat = max(0, self.roll.cursor_beat + direction / self.roll.subdivision)
            self.roll.viewport().update()
            event.accept()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.player.stop()
        if self.dirty:
            choice = QMessageBox.question(
                self,
                style.tr("save"),
                "曲谱已修改，是否保存？",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if choice == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if choice == QMessageBox.StandardButton.Save and not self.save():
                event.ignore()
                return
        event.accept()

    def reject(self):
        self.close()
