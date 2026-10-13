from __future__ import annotations

import base64
import logging
from collections.abc import Callable

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRunnable,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

from . import style


def button(key: str, callback: Callable | None = None, accent: bool = False):
    widget = QPushButton(style.tr(key))
    widget.setProperty("tr_key", key)
    if callback:
        widget.clicked.connect(callback)
    if accent:
        widget.setObjectName("accent")
    return widget


def label(text: str, kind: str = ""):
    widget = QLabel(text)
    if kind:
        widget.setObjectName(kind)
    return widget


class AccountButton(QPushButton):
    doubleClicked = Signal()

    def __init__(self, text: str):
        super().__init__(text)
        self._feedback = 0.0
        self._ignore_release = False
        self.animation = QPropertyAnimation(self, b"feedback", self)
        self.animation.setDuration(200)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.pressed.connect(self.flash)

    def _get_feedback(self):
        return self._feedback

    def _set_feedback(self, value):
        self._feedback = value
        self.update()

    feedback = Property(float, _get_feedback, _set_feedback)

    def flash(self):
        self.animation.stop()
        self.animation.setStartValue(1.0)
        self.animation.setEndValue(0.0)
        self.animation.start()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._feedback > 0:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = QColor(style.colors["accent"])
            color.setAlphaF(0.18 * self._feedback)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)

    def mousePressEvent(self, event):
        self._ignore_release = False
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            # Confirmation can change login state before the second release.
            # Suppress that release so it cannot trigger the sign-in action.
            self._ignore_release = True
            self.setDown(False)
            self.flash()
            event.accept()
            self.doubleClicked.emit()
            return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        if self._ignore_release and event.button() == Qt.MouseButton.LeftButton:
            self._ignore_release = False
            self.setDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


def time_text(ms: float) -> str:
    seconds = int(max(0, ms) / 1000)
    return f"{seconds // 60}:{seconds % 60:02}"


def playback_icon(kind: str, active: bool = False) -> QIcon:
    pixmap = QPixmap(48, 48)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(style.colors["ink" if active else "text"]), 1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    def path(points):
        line = QPainterPath(QPointF(*points[0]))
        for point in points[1:]:
            line.lineTo(QPointF(*point))
        painter.drawPath(line)

    if kind == "sequence":
        for y in (6, 12, 18):
            painter.drawLine(3, y, 11, y)
        path([(16, 7), (21, 12), (16, 17)])
        painter.drawLine(14, 12, 21, 12)
    elif kind in ("list", "single"):
        path([(4, 10), (4, 5), (20, 5), (17, 2)])
        path([(20, 14), (20, 19), (4, 19), (7, 22)])
        if kind == "single":
            font = painter.font()
            font.setPixelSize(11)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(0, 0, 24, 24, Qt.AlignmentFlag.AlignCenter, "1")
    elif kind == "shuffle":
        path([(3, 6), (6, 6), (18, 18), (21, 18)])
        path([(3, 18), (6, 18), (18, 6), (21, 6)])
        path([(18, 3), (21, 6), (18, 9)])
        path([(18, 15), (21, 18), (18, 21)])
    elif kind == "queue":
        for y in (6, 12, 18):
            painter.drawPoint(3, y)
            painter.drawLine(8, y, 21, y)
    elif kind in ("volume", "muted"):
        path([(3, 9), (6, 9), (11, 5), (11, 19), (6, 15), (3, 15), (3, 9)])
        if kind == "muted":
            path([(16, 9), (21, 15)])
            path([(16, 15), (21, 9)])
        else:
            for x, top, bottom in ((14, 8, 16), (17, 5, 19)):
                wave = QPainterPath(QPointF(x, top))
                wave.quadTo(x + 6, 12, x, bottom)
                painter.drawPath(wave)
    painter.end()
    return QIcon(pixmap)


class VolumeButton(QPushButton):
    def __init__(self, value: int, overlay_parent: QWidget):
        super().__init__()
        self.setObjectName("transport")
        self.setFixedSize(44, 44)
        self.popup = QFrame(overlay_parent)
        self.popup.setObjectName("panel")
        self.popup.setFixedSize(72, 190)
        self.popup.installEventFilter(self)
        layout = QVBoxLayout(self.popup)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(10)
        self.level = label("", "muted")
        self.level.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.level)
        self.slider = QSlider(Qt.Orientation.Vertical)
        self.slider.setRange(0, 100)
        self.slider.setAccessibleName(style.tr("volume"))
        layout.addWidget(self.slider, 1, Qt.AlignmentFlag.AlignHCenter)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(150)
        self.hide_timer.timeout.connect(self.hide_if_outside)
        self.slider.valueChanged.connect(self.update_volume)
        self.slider.sliderPressed.connect(self.hide_timer.stop)
        self.slider.sliderReleased.connect(lambda: self.hide_timer.start())
        self.slider.setValue(value)
        self.update_volume(value)
        self.clicked.connect(lambda: self.show_popup(focus=True))
        self.popup.hide()

    def update_volume(self, value):
        self.level.setText(f"{value}%")
        self.setToolTip(f"{style.tr('volume')}: {value}%")
        self.setAccessibleName(self.toolTip())
        self.setIcon(playback_icon("volume" if value else "muted"))

    def position_popup(self):
        parent = self.popup.parentWidget()
        point = self.mapTo(parent, QPoint(0, 0))
        x = max(16, min(
            point.x() + (self.width() - self.popup.width()) // 2,
            parent.width() - self.popup.width() - 16,
        ))
        self.popup.move(x, max(16, point.y() - self.popup.height() - 8))

    def show_popup(self, focus=False):
        self.hide_timer.stop()
        self.position_popup()
        self.popup.show()
        self.popup.raise_()
        if focus:
            self.slider.setFocus(Qt.FocusReason.OtherFocusReason)

    def hide_popup(self):
        self.hide_timer.stop()
        if self.slider.hasFocus():
            self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.popup.hide()

    def hide_if_outside(self):
        if not (self.underMouse() or self.popup.underMouse() or self.slider.isSliderDown()):
            self.hide_popup()

    def enterEvent(self, event):
        self.show_popup()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hide_timer.start()
        super().leaveEvent(event)

    def eventFilter(self, watched, event):
        if watched is self.popup:
            if event.type() == QEvent.Type.Enter:
                self.hide_timer.stop()
            elif event.type() == QEvent.Type.Leave:
                self.hide_timer.start()
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            if hasattr(self, "slider"):
                self.update_volume(self.slider.value())


def cover_icon(score) -> QIcon:
    cover = score.root.get("cover", "")
    pixmap = QPixmap()
    if isinstance(cover, str) and cover:
        try:
            pixmap.loadFromData(
                base64.b64decode(cover.split(",", 1)[-1] if cover.startswith("data:") else cover)
            )
        except (ValueError, TypeError):
            pass
    if pixmap.isNull():
        pixmap = QPixmap(56, 56)
        pixmap.fill(QColor(style.colors["raised"]))
        painter = QPainter(pixmap)
        painter.setPen(QColor(style.colors["accent"]))
        font = painter.font()
        font.setPixelSize(29)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "♪")
        painter.end()
    return QIcon(pixmap)


class JobSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()
    progress = Signal(int)


class Job(QRunnable):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.signals = JobSignals()

    def run(self):
        try:
            self.signals.result.emit(self.callback())
        except Exception as exc:
            logging.getLogger(__name__).exception("后台任务失败")
            self.signals.error.emit(str(exc))
        finally:
            self.signals.finished.emit()


class DiamondKey(QWidget):
    pressed = Signal(int)
    released = Signal(int)

    def __init__(self, index: int, binding: str):
        super().__init__()
        self.index, self.binding = index, binding
        self.down = False
        self.expected = 0
        self._pulse = 0.0
        self.setMinimumSize(48, 55)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"{binding} · Key {index}")
        self.animation = QPropertyAnimation(self, b"pulse", self)
        self.animation.setDuration(350)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_pulse(self):
        return self._pulse

    def _set_pulse(self, value):
        self._pulse = value
        self.update()

    pulse = Property(float, _get_pulse, _set_pulse)

    def flash(self):
        self.animation.stop()
        self.animation.setStartValue(1.0)
        self.animation.setEndValue(0.0)
        self.animation.start()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = style.colors
        size = min(self.width(), self.height()) * 0.39
        center = QPointF(self.width() / 2, self.height() / 2)
        diamond = QPolygonF(
            [
                center + QPointF(0, -size),
                center + QPointF(size, 0),
                center + QPointF(0, size),
                center + QPointF(-size, 0),
            ]
        )
        color = QColor(c["accent"] if self.down or self.expected == 2 or self._pulse > 0.1 else c["raised"])
        if self.expected == 1:
            color = QColor(c["accent"])
            color.setAlpha(65)
        p.setBrush(color)
        p.setPen(QPen(QColor(c["accent"] if self.expected else c["border"]), 1.5))
        p.drawPolygon(diamond)
        p.setPen(QColor(c["ink"] if self.down or self.expected == 2 or self._pulse > 0.1 else c["text"]))
        font = p.font()
        font.setPixelSize(max(13, int(size * 0.6)))
        font.setBold(True)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.binding)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.down = True
            self.flash()
            self.pressed.emit(self.index)
            self.update()

    def mouseReleaseEvent(self, event):
        if self.down:
            self.down = False
            self.released.emit(self.index)
            self.update()


class Piano(QWidget):
    pressed = Signal(int)
    released = Signal(int)

    def __init__(self, bindings):
        super().__init__()
        layout = QGridLayout(self)
        layout.setSpacing(5)
        self.keys = []
        for i, binding in enumerate(bindings):
            key = DiamondKey(i, binding)
            key.pressed.connect(self.pressed)
            key.released.connect(self.released)
            layout.addWidget(key, i // 5, i % 5)
            self.keys.append(key)

    def flash(self, keys):
        for key in keys:
            self.keys[key].flash()

    def hints(self, current: set[int], following: set[int]):
        for i, key in enumerate(self.keys):
            key.expected = 2 if i in current else 1 if i in following else 0
            key.update()

    def release_all(self):
        for i, key in enumerate(self.keys):
            if key.down:
                key.down = False
                self.released.emit(i)
                key.update()


class ScoreWall(QWidget):
    def __init__(self, practice):
        super().__init__()
        self.practice = practice
        self.setMinimumHeight(250)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = style.colors
        width, height = self.width() / 8, self.height() / 4
        page = min(max(0, len(self.practice.groups) - 1), self.practice.index) // 32
        for cell in range(32):
            index = page * 32 + cell
            x, y = (cell % 8) * width, (cell // 8) * height
            p.setBrush(QColor(colors["raised"] if index == self.practice.index else colors["panel"]))
            p.setPen(QColor(colors["accent"] if index == self.practice.index else colors["border"]))
            p.drawRoundedRect(int(x + 4), int(y + 4), int(width - 8), int(height - 8), 7, 7)
            if index >= len(self.practice.groups):
                continue
            keys = self.practice.groups[index][1]
            for key in range(15):
                p.setBrush(QColor(colors["accent"] if key in keys else colors["border"]))
                p.setPen(Qt.PenStyle.NoPen)
                pos = QPointF(x + width * (0.20 + (key % 5) * 0.15), y + height * (0.25 + (key // 5) * 0.23))
                p.drawEllipse(pos, min(4.5, width / 22), min(4.5, width / 22))
