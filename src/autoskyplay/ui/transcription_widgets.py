"""Native waveform selection and a strictly read-only Sky piano roll."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QWidget

from ..score import Score
from . import style
from .editor import PianoRoll


def clock_text(seconds):
    seconds = max(0, seconds)
    return f'{int(seconds // 60):02d}:{seconds % 60:05.2f}'


class Waveform(QWidget):
    range_changed = Signal(float, float)
    seek_requested = Signal(float)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(108)
        self.setAccessibleName('原曲波形与扒谱选段')
        self.data = None
        self.start = self.end = self.cursor = 0
        self.view_start, self.view_end = 0, 30
        self.drag = None
        self.setMouseTracking(True)

    def set_data(self, data):
        self.data = data
        self.view_start = 0
        self.view_end = max(data['origin'] + data['duration'], data['full_duration'], .1)
        self.update()

    def set_range(self, start, end):
        self.start, self.end = start, end
        self.update()

    def x(self, seconds):
        return 12 + (seconds - self.view_start) / (self.view_end - self.view_start) * max(1, self.width() - 24)

    def time_at(self, x):
        return self.view_start + (x - 12) / max(1, self.width() - 24) * (self.view_end - self.view_start)

    def zoom(self, factor):
        if not self.data:
            return
        total = max(self.data['full_duration'], self.data['origin'] + self.data['duration'])
        center = (self.start + self.end) / 2
        span = min(total, max(2, (self.view_end - self.view_start) * factor))
        self.view_start = max(0, min(total - span, center - span / 2))
        self.view_end = self.view_start + span
        self.update()

    def fit(self):
        if self.data:
            self.set_data(self.data)

    def paintEvent(self, event):
        p = QPainter(self)
        c = style.colors
        p.fillRect(self.rect(), QColor(c['panel']))
        if not self.data:
            p.setPen(QColor(c['muted']))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, '选择歌曲后，加载真实波形并试听选段')
            return
        origin, duration = self.data['origin'], self.data['duration']
        p.fillRect(QRectF(self.x(origin), 24, self.x(origin + duration) - self.x(origin), 68), QColor(c['raised']))
        peaks = self.data['peaks']
        p.setPen(QPen(QColor(c['muted']), 1))
        for x in range(12, self.width() - 12):
            t = self.time_at(x)
            if origin <= t < origin + duration and peaks:
                i = max(0, int((t - origin) / duration * len(peaks)))
                j = min(len(peaks), max(i + 1, int((self.time_at(x + 1) - origin) / duration * len(peaks))))
                height = max(1, max(peaks[i:j], default=0) * 29)
                p.drawLine(QPointF(x, 58 - height), QPointF(x, 58 + height))
        tint = QColor(c['accent'])
        tint.setAlpha(45)
        p.fillRect(QRectF(self.x(self.start), 24, self.x(self.end) - self.x(self.start), 68), tint)
        p.setPen(QPen(QColor(c['accent']), 2))
        for t in (self.start, self.end):
            x = self.x(t)
            p.drawLine(QPointF(x, 24), QPointF(x, 92))
            p.fillRect(QRectF(x - 4, 40, 8, 32), QColor(c['accent']))
        p.setPen(QColor(c['muted']))
        for i in range(7):
            t = self.view_start + i / 6 * (self.view_end - self.view_start)
            x = self.x(t)
            p.drawText(QRectF(max(0, min(self.width() - 65, x - 25)), 1, 65, 20), clock_text(t))
        p.setPen(QPen(QColor(c['ink']), 1.5))
        p.drawLine(QPointF(self.x(self.cursor), 22), QPointF(self.x(self.cursor), 96))

    def mousePressEvent(self, event):
        if not self.data or event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x()
        t = self.time_at(x)
        if abs(x - self.x(self.start)) < 10:
            self.drag = ('start', t, self.start, self.end)
        elif abs(x - self.x(self.end)) < 10:
            self.drag = ('end', t, self.start, self.end)
        elif self.start <= t <= self.end:
            self.drag = ('move', t, self.start, self.end)
        else:
            self.drag = ('seek', t, self.start, self.end)
        self.press_x = x

    def mouseMoveEvent(self, event):
        if not self.drag:
            return
        kind, t, start, end = self.drag
        delta = self.time_at(event.position().x()) - t
        low, high = self.data['origin'], self.data['origin'] + self.data['duration']
        if kind == 'start':
            start = max(low, end - 600, min(end - .1, start + delta))
        elif kind == 'end':
            end = min(high, start + 600, max(start + .1, end + delta))
        elif kind == 'move' and abs(event.position().x() - self.press_x) > 3:
            delta = max(low - start, min(high - end, delta))
            start, end = start + delta, end + delta
        else:
            return
        self.set_range(start, end)
        self.range_changed.emit(start, end)

    def mouseReleaseEvent(self, event):
        if self.drag and abs(event.position().x() - self.press_x) <= 3 and self.drag[0] in ('seek', 'move'):
            low, high = self.data['origin'], self.data['origin'] + self.data['duration']
            self.seek_requested.emit(max(low, min(high, self.time_at(event.position().x()))))
        self.drag = None

    def wheelEvent(self, event):
        if not self.data:
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(.8 if event.angleDelta().y() > 0 else 1.25)
        else:
            span = self.view_end - self.view_start
            total = max(self.data['full_duration'], self.data['origin'] + self.data['duration'])
            self.view_start = max(0, min(total - span, self.view_start - event.angleDelta().y() / 120 * span / 10))
            self.view_end = self.view_start + span
            self.update()
        event.accept()


class PreviewRoll(PianoRoll):
    seek_requested = Signal(float)
    rows_resized = Signal(int)

    def __init__(self):
        super().__init__(Score())
        self.setMinimumHeight(170)
        self.row_height = 22
        self.grid_offset_ms = 0
        self.follow = True
        self.clip_duration = 0
        self.setDragMode(self.DragMode.ScrollHandDrag)
        self.setAccessibleName('只读光遇曲谱预览')

    def rebuild(self):
        super().rebuild()
        for item in self.scene().items():
            item.setFlags(QGraphicsItem.GraphicsItemFlag(0))
            note = item.note
            item.setToolTip(f'键位 {note.key + 1} · {note.time_ms / 1000:.2f} 秒 · {note.duration_ms:.0f} ms'
                            + (' · 半音近似' if note.extra.get('approximated') else '')
                            + (' · 音域折叠' if note.extra.get('folded') else ''))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.row_height = max(16, (self.viewport().height() - 2) // 15)
        self.rows_resized.emit(self.row_height)
        if hasattr(self, "clip_duration"):
            self.set_score(self.score, self.clip_duration)

    def set_score(self, score, duration=0):
        self.score = score or Score()
        self.clip_duration = max(duration, self.score.duration_ms)
        self.total_beats = max(4, math.ceil(self.clip_duration / self.score.ms_per_beat) + 1)
        self.rebuild()
        self.scene().setSceneRect(0, 0, self.total_beats * self.pixels_per_beat, 15 * self.row_height)

    def fit(self):
        self.pixels_per_beat = max(2, (self.viewport().width() - 12) / self.total_beats)
        self.set_score(self.score, self.clip_duration)

    def zoom(self, factor):
        self.pixels_per_beat = max(2, min(400, self.pixels_per_beat * factor))
        self.set_score(self.score, self.clip_duration)

    def set_position(self, ms):
        self.cursor_beat = ms / self.score.ms_per_beat
        if self.follow:
            self.ensureVisible(QRectF(self.cursor_beat * self.pixels_per_beat, 0, 1, 1), 40, 0)
        self.viewport().update()

    def drawBackground(self, painter, rect):
        c = style.colors
        painter.fillRect(rect, QColor(c['panel']))
        for row in range(15):
            if row % 2 == 0:
                painter.fillRect(QRectF(rect.left(), row * self.row_height, rect.width(), self.row_height), QColor(c['raised']))
            painter.setPen(QColor(c['border']))
            painter.drawLine(QPointF(rect.left(), row * self.row_height), QPointF(rect.right(), row * self.row_height))
        step = self.pixels_per_beat / self.subdivision
        offset = getattr(self, 'grid_offset_ms', 0) / self.score.ms_per_beat * self.pixels_per_beat
        for i in range(int((rect.left() - offset) / step) - 1, int((rect.right() - offset) / step) + 2):
            if step < 6 and i % self.subdivision:
                continue
            painter.setPen(QPen(QColor(c['muted'] if i % (4 * self.subdivision) == 0 else c['border']), .7))
            painter.drawLine(QPointF(offset + i * step, 0), QPointF(offset + i * step, 15 * self.row_height))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            ms = self.mapToScene(event.position().toPoint()).x() / self.pixels_per_beat * self.score.ms_per_beat
            self.seek_requested.emit(max(0, min(self.clip_duration, ms)))
        # Never call the editable superclass's event handlers.
        event.accept()

    def mouseMoveEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def keyPressEvent(self, event):
        event.ignore()

    def add_note(self, *args):
        pass

    def delete_selected(self):
        pass


class RollKeys(QWidget):
    def __init__(self, roll):
        super().__init__()
        self.roll = roll
        self.setFixedWidth(22)
        roll.rows_resized.connect(lambda _: self.update())
        roll.verticalScrollBar().valueChanged.connect(lambda _: self.update())

    def paintEvent(self, event):
        p = QPainter(self)
        p.setPen(QColor(style.colors['muted']))
        offset = self.roll.verticalScrollBar().value()
        keys = ['1', '2', '3', '4', '5', '6', '7', '1′', '2′', '3′', '4′', '5′', '6′', '7′', '1″']
        for index, key in enumerate(keys):
            y = index * self.roll.row_height - offset
            if 0 <= y < self.roll.viewport().height():
                p.drawText(QRectF(0, y, 22, self.roll.row_height), Qt.AlignmentFlag.AlignCenter, key)
