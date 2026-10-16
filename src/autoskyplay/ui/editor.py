from __future__ import annotations

import base64
import copy
import math

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from ..midi import SKY_MIDI
from ..playback import Player, State
from ..score import Note, Score, unique_path
from . import style
from .netease import NeteaseSongDialog
from .widgets import button, label, time_text


def pitch_name(key: int, offset: int = 0) -> str:
    midi = SKY_MIDI[key] + offset
    return f"{('C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B')[midi % 12]}{midi // 12 - 1}"


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
            note.time_ms / roll.score.ms_per_beat * roll.pixels_per_beat, roll.key_y(note.key) + 3
        )
        self.snapping = True

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(style.colors["muted" if self.note.extra.get("role") == "accompaniment" else "accent"]))
        painter.setPen(QPen(QColor(style.colors["text"] if self.isSelected() else style.colors["ink"]),
                            2 if self.isSelected() else 1))
        painter.drawRoundedRect(self.rect(), 3, 3)

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
                key = 14 - int(round((item.y() - 3) / self.roll.row_height))
                if item.note.time_ms != time_ms or item.note.key != key:
                    changed = True
                item.note.time_ms, item.note.key = time_ms, key
        if changed:
            self.roll.changed.emit()


class PianoRoll(QGraphicsView):
    changed = Signal()
    cursor_changed = Signal(float)
    extent_changed = Signal()
    viewport_changed = Signal()

    def __init__(self, score: Score):
        super().__init__()
        self.score = score
        self.pixels_per_beat = 70
        self.row_height = 22
        self.subdivision = 4
        self.cursor_beat = 0.0
        self.total_beats = max(32, math.ceil(score.duration_ms / score.ms_per_beat) + 8)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumHeight(15 * self.row_height + 18)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.rebuild()

    def key_y(self, key: int) -> float:
        return (14 - key) * self.row_height

    def set_cursor(self, beat: float):
        self.cursor_beat = min(self.total_beats, max(0, beat))
        self.viewport().update()
        self.cursor_changed.emit(self.cursor_beat)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.viewport_changed.emit()

    def rebuild(self):
        self.scene().clear()
        self.total_beats = max(
            self.total_beats, math.ceil(self.score.duration_ms / self.score.ms_per_beat) + 8
        )
        self.scene().setSceneRect(0, 0, self.total_beats * self.pixels_per_beat, 15 * self.row_height)
        for note in self.score.notes:
            self.scene().addItem(NoteItem(note, self))
        self.viewport().update()
        self.extent_changed.emit()

    def drawBackground(self, painter, rect):
        c = style.colors
        painter.fillRect(rect, QColor(c["panel"]))
        for row in range(15):
            if (row // 5) % 2 == 0:
                painter.fillRect(
                    QRectF(rect.left(), row * self.row_height, rect.width(), self.row_height),
                    QColor(c["raised"]),
                )
            painter.setPen(QPen(QColor(c["muted"] if row % 5 == 0 else c["border"]), 0.8))
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
        painter.setPen(QPen(QColor("#f4778d"), 2))
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
            key = 14 - int(position.y() // self.row_height)
            beat = max(0, round(position.x() / self.pixels_per_beat * self.subdivision) / self.subdivision)
            if 0 <= key < 15:
                self.set_cursor(beat)
                self.add_note(key, beat)
            return
        super().mousePressEvent(event)

    def delete_selected(self):
        notes = [item.note for item in self.scene().selectedItems() if isinstance(item, NoteItem)]
        if notes:
            self.score.notes = [n for n in self.score.notes if not any(n is old for old in notes)]
            self.rebuild()
            self.changed.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Space) or event.text():
            event.ignore()
        else:
            super().keyPressEvent(event)

    def follow_cursor(self):
        y = self.mapToScene(0, self.viewport().height() // 2).y()
        self.ensureVisible(self.cursor_beat * self.pixels_per_beat, y, 8, 1, 40, 0)


class ProgressSlider(QSlider):
    """A click on the track seeks directly, just like dragging the handle."""

    def __init__(self):
        super().__init__(Qt.Orientation.Horizontal)

    def seek_at(self, event):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self,
        )
        value = QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), round(event.position().x() - handle.width() / 2),
            max(1, self.width() - handle.width()), option.upsideDown,
        )
        self.setValue(value)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self.seek_at(event)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.seek_at(event)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.seek_at(event)
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class PitchAxis(QWidget):
    def __init__(self, roll: PianoRoll, bindings, offset: int = 0):
        super().__init__()
        self.roll, self.bindings, self.offset = roll, bindings, offset
        self.setFixedWidth(116)
        self.setToolTip("音高 · 光遇键位 · 键盘按键")
        roll.verticalScrollBar().valueChanged.connect(self.update)
        roll.viewport_changed.connect(self.update)

    def paintEvent(self, event):
        painter = QPainter(self)
        c = style.colors
        painter.fillRect(self.rect(), QColor(c["panel"]))
        painter.setClipRect(0, 0, self.width(), self.roll.viewport().height())
        for key in range(15):
            y = self.roll.mapFromScene(QPointF(0, self.roll.key_y(key))).y()
            rect = QRectF(0, y, self.width(), self.roll.row_height)
            if key % 7 == 0:
                painter.fillRect(rect, QColor(c["raised"]))
            painter.setPen(QColor(c["border"]))
            painter.drawLine(QPointF(0, y), QPointF(self.width(), y))
            painter.setPen(QColor(c["accent"] if key % 7 == 0 else c["text"]))
            painter.drawText(rect.adjusted(9, 0, -70, 0), Qt.AlignmentFlag.AlignVCenter, pitch_name(key, self.offset))
            painter.setPen(QColor(c["muted"]))
            painter.drawText(rect.adjusted(48, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter,
                             f"K{key}  {self.bindings[key]}")


class BeatRuler(QWidget):
    seek_requested = Signal(float)

    def __init__(self, roll: PianoRoll):
        super().__init__()
        self.roll = roll
        self.setFixedHeight(28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击或拖动拍数标尺定位播放光标")
        roll.horizontalScrollBar().valueChanged.connect(self.update)
        roll.viewport_changed.connect(self.update)
        roll.cursor_changed.connect(self.update)

    def paintEvent(self, event):
        painter = QPainter(self)
        c = style.colors
        painter.fillRect(self.rect(), QColor(c["panel"]))
        painter.setClipRect(0, 0, self.roll.viewport().width(), self.height())
        first = max(0, math.floor(self.roll.mapToScene(0, 0).x() / self.roll.pixels_per_beat))
        last = min(self.roll.total_beats, first + math.ceil(self.width() / self.roll.pixels_per_beat) + 1)
        for beat in range(first, last + 1):
            x = self.roll.mapFromScene(QPointF(beat * self.roll.pixels_per_beat, 0)).x()
            painter.setPen(QColor(c["text"] if beat % 4 == 0 else c["muted"]))
            painter.drawText(QRectF(x + 5, 0, 55, 22), Qt.AlignmentFlag.AlignVCenter, str(beat + 1))
            painter.drawLine(x, 23 if beat % 4 else 19, x, 28)
        x = self.roll.mapFromScene(QPointF(self.roll.cursor_beat * self.roll.pixels_per_beat, 0)).x()
        painter.fillRect(QRectF(x - 3, 0, 6, self.height()), QColor("#f4778d"))

    def seek_at(self, event):
        x = self.roll.mapToScene(int(event.position().x()), 0).x()
        beat = round(x / self.roll.pixels_per_beat * self.roll.subdivision) / self.roll.subdivision
        self.seek_requested.emit(beat)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.seek_at(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.seek_at(event)


def encode_cover(image):
    image = image.scaled(512, 512, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "JPEG", 80):
        raise ValueError("封面压缩失败，请使用其他图片")
    buffer.close()
    return base64.b64encode(bytes(data)).decode("ascii")


class ScoreInfoDialog(QDialog):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor, self.host = editor, editor.host
        self.setWindowTitle("编辑曲谱信息")
        self.setMinimumWidth(390)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(8)
        title = label("编辑曲谱信息")
        title.setObjectName("infoTitle")
        root.addWidget(title)
        self.name = QLineEdit()
        self.author = QLineEdit()
        self.transcriber = QLineEdit()
        for text, field in [("曲名", self.name), ("作者（原唱 / 作曲）", self.author),
                            ("做谱者", self.transcriber)]:
            root.addWidget(label(text))
            field.setAccessibleName(text)
            root.addWidget(field)
        root.addWidget(label("封面"))
        cover_row = QHBoxLayout()
        self.cover_preview = QLabel()
        self.cover_preview.setObjectName("coverPreview")
        self.cover_preview.setFixedSize(84, 84)
        self.cover_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover_row.addWidget(self.cover_preview)
        cover_controls = QVBoxLayout()
        actions = QHBoxLayout()
        self.choose_cover = QPushButton("选择封面")
        self.choose_cover.clicked.connect(self.set_cover)
        self.remove_cover = QPushButton("移除")
        self.remove_cover.clicked.connect(self.clear_cover)
        actions.addWidget(self.choose_cover)
        actions.addWidget(self.remove_cover)
        actions.addStretch()
        cover_controls.addLayout(actions)
        cover_controls.addWidget(label("自动压缩，最长边 512", "muted"))
        cover_controls.addStretch()
        cover_row.addLayout(cover_controls, 1)
        root.addLayout(cover_row)
        self.netease_button = QPushButton("从网易云获取")
        self.netease_button.clicked.connect(self.fetch_netease)
        root.addWidget(self.netease_button)
        self.netease_status = label("填写曲名、歌手与专辑封面", "muted")
        self.netease_status.setTextFormat(Qt.TextFormat.PlainText)
        self.netease_status.setWordWrap(True)
        root.addWidget(self.netease_status)
        footer = QHBoxLayout()
        footer.addStretch()
        self.confirm = QPushButton("确定")
        self.confirm.setObjectName("accent")
        self.confirm.clicked.connect(self.accept)
        self.cancel = button("cancel", self.reject)
        footer.addWidget(self.confirm)
        footer.addWidget(self.cancel)
        root.addLayout(footer)
        self.reload()

    def reload(self):
        score = self.editor.score
        self.name.setText(score.name)
        self.author.setText(score.author)
        self.transcriber.setText(score.transcriber)
        self.cover = score.root.get("cover")
        self.netease_status.setText("填写曲名、歌手与专辑封面")
        self.update_cover()

    def update_cover(self):
        pixmap = QPixmap()
        try:
            if self.cover:
                pixmap.loadFromData(base64.b64decode(self.cover))
        except (ValueError, TypeError):
            pass
        self.cover_preview.clear()
        if pixmap.isNull():
            self.cover_preview.setText("无封面")
        else:
            self.cover_preview.setPixmap(pixmap.scaled(
                80, 80, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            ))
        self.remove_cover.setEnabled(bool(self.cover))

    def set_cover(self):
        path, _ = QFileDialog.getOpenFileName(
            self, style.tr("cover"), "", "Images (*.png *.jpg *.jpeg *.webp)",
        )
        if not path:
            return
        image = QImage(path)
        if image.isNull():
            self.host.show_error("图片读取失败")
            return
        self.embed_cover(image)

    def embed_cover(self, image):
        try:
            self.cover = encode_cover(image)
        except ValueError as exc:
            self.host.show_error(str(exc))
            return False
        self.update_cover()
        return True

    def clear_cover(self):
        self.cover = None
        self.update_cover()

    def fetch_netease(self):
        dialog = NeteaseSongDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selection:
            self.apply_netease(*dialog.selection)

    def apply_netease(self, song, cover):
        if cover:
            image = QImage.fromData(cover)
            if image.isNull():
                self.host.show_error("网易云专辑封面图片无效，请重试或手动设置封面")
                return False
            if not self.embed_cover(image):
                return False
        self.name.setText(song.name)
        self.author.setText(song.artists)
        self.netease_status.setText(f"已填写：{song.name} · {song.album} · ID {song.id}")
        return True

    def accept(self):
        score = self.editor.score
        values = (self.name.text().strip() or "未命名", self.author.text().strip(),
                  self.transcriber.text().strip())
        changed = values != (score.name, score.author, score.transcriber) or self.cover != score.root.get("cover")
        score.name, score.author, score.transcriber = values
        if self.cover is None:
            score.root.pop("cover", None)
        else:
            score.root["cover"] = self.cover
        self.reload()
        if changed:
            self.editor.mark_dirty()
        self.editor.update_summary()
        super().accept()

    def reject(self):
        self.reload()
        super().reject()


class EditorDialog(QDialog):
    note_fired = Signal(object)
    error = Signal(str)

    def __init__(self, parent, score: Score, draft_mode=False):
        super().__init__(parent)
        self.host = parent
        self.draft_mode = draft_mode
        self.score = copy.deepcopy(score)
        self.saved = False
        self.dirty = False
        self.history = [copy.deepcopy(self.score.notes)]
        self.history_index = 0
        self.player = Player(self.note_fired.emit, on_error=self.error.emit)
        self.note_fired.connect(self.on_preview_notes)
        self.error.connect(parent.show_error)
        self.resize(1180, 760)
        self.setWindowTitle(style.tr("edit"))
        self.setObjectName("scoreEditor")
        self.setStyleSheet("""
            QFrame#editorToolbar, QFrame#editorKeyboard {
                background: palette(button); border-radius: 8px;
            }
            QLabel#editorTitle { font-size: 17px; font-weight: 700; }
            QLabel#infoTitle { font-size: 17px; font-weight: 700; padding-bottom: 6px; }
            QLabel#coverPreview { background: palette(base); border: 1px solid palette(mid); border-radius: 8px; }
            QPushButton#editorKey { padding: 3px 8px; }
            QPushButton#editorKey:pressed { background: palette(highlight); color: palette(highlighted-text); }
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)
        title_row = QHBoxLayout()
        title_row.addWidget(label("钢琴卷帘编辑器", "editorTitle"))
        self.song_summary = label(self.score.name, "muted")
        self.song_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.song_summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.song_summary.setToolTip(self.score.name)
        title_row.addWidget(self.song_summary, 1)
        root.addLayout(title_row)

        self.info_dialog = ScoreInfoDialog(self)
        self.name = self.info_dialog.name
        self.author = self.info_dialog.author
        self.transcriber = self.info_dialog.transcriber
        self.netease_button = self.info_dialog.netease_button
        self.netease_status = self.info_dialog.netease_status
        self.roll = PianoRoll(self.score)

        toolbar_panel = QFrame()
        toolbar_panel.setObjectName("editorToolbar")
        toolbar = QHBoxLayout(toolbar_panel)
        toolbar.setContentsMargins(8, 7, 8, 7)
        toolbar.setSpacing(7)
        self.previous_beat = QPushButton("◀")
        self.previous_beat.setToolTip("向前移动一格 · ←")
        self.previous_beat.clicked.connect(lambda: self.move_cursor(-1))
        self.preview_button = QPushButton("▶")
        self.preview_button.setObjectName("accent")
        self.preview_button.setToolTip("播放 / 暂停 · Space")
        self.preview_button.setAccessibleName("播放 / 暂停")
        self.preview_button.clicked.connect(self.preview)
        self.next_beat = QPushButton("▶")
        self.next_beat.setToolTip("向后移动一格 · →")
        self.next_beat.clicked.connect(lambda: self.move_cursor(1))
        self.reset_button = QPushButton("|◀")
        self.reset_button.setToolTip("停止并回到开头")
        self.reset_button.clicked.connect(lambda: self.stop_preview(rewind=True))
        for control in (self.previous_beat, self.preview_button, self.next_beat, self.reset_button):
            control.setFixedWidth(40)
            control.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            control.setAccessibleName(control.toolTip())
            toolbar.addWidget(control)
        toolbar.addWidget(label("音色", "muted"))
        self.instrument = QComboBox()
        for index in range(self.host.instrument.count()):
            self.instrument.addItem(self.host.instrument.itemText(index), self.host.instrument.itemData(index))
        self.instrument.setCurrentIndex(self.host.instrument.currentIndex())
        self.instrument.setMaximumWidth(155)
        self.instrument.setAccessibleName("试听音色")
        self.instrument.currentIndexChanged.connect(self.change_instrument)
        toolbar.addWidget(self.instrument)
        toolbar.addWidget(label("BPM", "muted"))
        self.bpm = QDoubleSpinBox()
        self.bpm.setRange(1, 2000)
        self.bpm.setDecimals(2)
        self.bpm.setValue(self.score.bpm)
        self.bpm.setMaximumWidth(95)
        self.bpm.setAccessibleName("BPM")
        self.bpm.valueChanged.connect(self.change_bpm)
        toolbar.addWidget(self.bpm)
        toolbar.addWidget(label(style.tr("grid"), "muted"))
        self.grid = QComboBox()
        for text, value in [("1/4 四分", 1), ("1/8 八分", 2), ("1/16 十六分", 4),
                            ("1/32 三十二分", 8), ("1/8 三连音", 3), ("1/16 三连音", 6)]:
            self.grid.addItem(text, value)
        self.grid.setCurrentIndex(2)
        self.grid.setAccessibleName(style.tr("grid"))
        self.grid.currentIndexChanged.connect(self.change_grid)
        toolbar.addWidget(self.grid)
        self.note_summary = label("", "muted")
        toolbar.addWidget(self.note_summary)
        toolbar.addStretch()
        self.save_button = button("save", self.save, accent=True)
        if draft_mode:
            self.save_button.setText("完成并返回草稿")
        toolbar.addWidget(self.save_button)
        if not draft_mode:
            toolbar.addWidget(button("saveas", lambda: self.save(True)))
        self.info_button = QPushButton("编辑信息")
        self.info_button.clicked.connect(self.edit_info)
        toolbar.addWidget(self.info_button)
        root.addWidget(toolbar_panel)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(button("undo", self.undo))
        controls.addWidget(button("redo", self.redo))
        controls.addWidget(button("delete", self.roll.delete_selected))
        controls.addSpacing(10)
        controls.addWidget(label(style.tr("notelength"), "muted"))
        self.note_length = QDoubleSpinBox()
        self.note_length.setRange(0.03125, 16)
        self.note_length.setValue(0.25)
        self.note_length.setSingleStep(0.25)
        self.note_length.setMaximumWidth(95)
        controls.addWidget(self.note_length)
        controls.addWidget(button("apply", self.apply_note_length))
        controls.addStretch()
        controls.addWidget(label(style.tr("beats"), "muted"))
        self.length = QSpinBox()
        self.length.setRange(8, max(10000, self.roll.total_beats))
        self.length.setValue(self.roll.total_beats)
        self.length.valueChanged.connect(self.change_length)
        controls.addWidget(self.length)
        root.addLayout(controls)

        progress = QHBoxLayout()
        progress.addWidget(label("播放进度", "muted"))
        self.progress = ProgressSlider()
        self.progress.setSingleStep(250)
        self.progress.setPageStep(1000)
        self.progress.setAccessibleName("编辑器播放进度")
        self.progress.setToolTip("拖动定位播放光标，可从当前位置试听")
        self.progress.valueChanged.connect(lambda value: self.roll.set_cursor(value / 1000))
        progress.addWidget(self.progress, 1)
        self.position_label = label("", "muted")
        self.position_label.setMinimumWidth(150)
        self.position_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        progress.addWidget(self.position_label)
        root.addLayout(progress)

        roll_layout = QGridLayout()
        roll_layout.setSpacing(0)
        corner = label("音高 / 键位", "muted")
        corner.setContentsMargins(9, 0, 0, 0)
        roll_layout.addWidget(corner, 0, 0)
        self.pitch_axis = PitchAxis(self.roll, self.host.store.data["keys"], self.host.pitch_value)
        self.ruler = BeatRuler(self.roll)
        self.ruler.seek_requested.connect(self.roll.set_cursor)
        roll_layout.addWidget(self.ruler, 0, 1)
        roll_layout.addWidget(self.pitch_axis, 1, 0)
        roll_layout.addWidget(self.roll, 1, 1)
        roll_layout.setColumnStretch(1, 1)
        roll_layout.setRowStretch(1, 1)
        root.addLayout(roll_layout, 1)
        help_label = label(
            "左键写入 · 拖动音符移动 · 右键删除 · Ctrl+拖动框选 · 15 键写谱 · ←/→ 移动光标 · Ctrl+Z/Y 撤销/重做 · Ctrl+S 保存 · Space 播放",
            "muted",
        )
        help_label.setWordWrap(True)
        root.addWidget(help_label)

        keyboard_panel = QFrame()
        keyboard_panel.setObjectName("editorKeyboard")
        keyboard_layout = QVBoxLayout(keyboard_panel)
        keyboard_layout.setContentsMargins(8, 7, 8, 8)
        keyboard_layout.setSpacing(5)
        keyboard_layout.addWidget(label("光遇按键映射 · 点击试听 / 播放时同步亮起", "muted"),
                                  alignment=Qt.AlignmentFlag.AlignHCenter)
        keys_widget = QWidget()
        keys_layout = QGridLayout(keys_widget)
        keys_layout.setContentsMargins(0, 0, 0, 0)
        keys_layout.setSpacing(5)
        self.key_buttons = []
        self.key_timers = []
        for key, binding in enumerate(self.host.store.data["keys"]):
            key_button = QPushButton(f"K{key}  {binding}")
            key_button.setObjectName("editorKey")
            key_button.setFixedSize(68, 30)
            key_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            key_button.setToolTip(f"{pitch_name(key, self.host.pitch_value)} · 点击试听")
            key_button.clicked.connect(lambda checked=False, key=key: self.audition_key(key))
            keys_layout.addWidget(key_button, key // 5, key % 5)
            self.key_buttons.append(key_button)
            timer = QTimer(key_button)
            timer.setSingleShot(True)
            timer.setInterval(140)
            timer.timeout.connect(lambda key=key: self.key_buttons[key].setDown(False))
            self.key_timers.append(timer)
        keyboard_layout.addWidget(keys_widget, alignment=Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(keyboard_panel)
        self.roll.changed.connect(self.record)
        self.roll.cursor_changed.connect(self.seek_playback)
        self.roll.extent_changed.connect(self.update_extent)
        self.update_extent()
        self.preview_state = self.player.state
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(33)
        self.preview_timer.timeout.connect(self.update_playback)
        self.preview_timer.start()
        self.finished.connect(self.preview_timer.stop)
        self.finished.connect(self.player.stop)
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
        for control in self.findChildren(QPushButton):
            control.setAutoDefault(False)

    def edit_info(self):
        self.info_dialog.reload()
        self.info_dialog.exec()

    def update_summary(self):
        self.song_summary.setText(self.score.name)
        self.song_summary.setToolTip(self.score.name)
        self.note_summary.setText(f"音符 {len(self.score.notes)} · {self.roll.total_beats} 拍")

    def update_extent(self):
        self.length.blockSignals(True)
        self.length.setMaximum(max(10000, self.roll.total_beats))
        self.length.setValue(self.roll.total_beats)
        self.length.blockSignals(False)
        self.progress.blockSignals(True)
        self.progress.setRange(0, self.roll.total_beats * 1000)
        self.progress.blockSignals(False)
        self.roll.cursor_beat = min(self.roll.cursor_beat, self.roll.total_beats)
        self.update_summary()
        self.update_position()

    def update_position(self):
        if not self.progress.isSliderDown():
            self.progress.blockSignals(True)
            self.progress.setValue(round(self.roll.cursor_beat * 1000))
            self.progress.blockSignals(False)
        self.position_label.setText(
            f"{time_text(self.roll.cursor_beat * self.score.ms_per_beat)} / "
            f"{time_text(self.roll.total_beats * self.score.ms_per_beat)} · {self.roll.cursor_beat + 1:.2f} 拍"
        )
        self.roll.viewport().update()
        self.ruler.update()

    def seek_playback(self, beat):
        if self.player.state in (State.PLAYING, State.PAUSED):
            self.player.seek(beat * self.score.ms_per_beat)
        self.roll.follow_cursor()
        self.update_position()

    def move_cursor(self, direction):
        self.roll.set_cursor(self.roll.cursor_beat + direction / self.roll.subdivision)

    def change_instrument(self):
        self.stop_preview()
        self.host.instrument.setCurrentIndex(self.host.instrument.findData(self.instrument.currentData()))
        self.pitch_axis.offset = self.host.pitch_value
        self.pitch_axis.update()
        for key, key_button in enumerate(self.key_buttons):
            key_button.setToolTip(f"{pitch_name(key, self.host.pitch_value)} · 点击试听")

    def flash_keys(self, keys):
        for key in keys:
            self.key_buttons[key].setDown(True)
            self.key_timers[key].start()

    def audition_key(self, key):
        if self.host.ensure_audio():
            self.host.mixer.trigger((key,))
            self.flash_keys((key,))

    def stop_preview(self, rewind=False):
        self.player.stop(rewind=rewind)
        if rewind:
            self.roll.set_cursor(0)
        self.update_playback()

    def update_playback(self):
        state = self.player.state
        if state == State.PLAYING or (state == State.FINISHED and self.preview_state == State.PLAYING):
            if not self.progress.isSliderDown():
                self.roll.cursor_beat = self.player.position_ms / self.score.ms_per_beat
                self.roll.follow_cursor()
                self.update_position()
        if state != self.preview_state:
            self.preview_button.setText("Ⅱ" if state == State.PLAYING else "▶")
        self.preview_state = state

    def mark_dirty(self):
        self.dirty = True
        self.setWindowTitle(style.tr("edit") + " *")

    def record(self):
        self.stop_preview()
        self.history = self.history[: self.history_index + 1]
        self.history.append(copy.deepcopy(self.score.notes))
        self.history_index += 1
        self.mark_dirty()

    def undo(self):
        if self.history_index > 0:
            self.stop_preview()
            self.history_index -= 1
            self.score.notes = copy.deepcopy(self.history[self.history_index])
            self.roll.rebuild()
            self.mark_dirty()

    def redo(self):
        if self.history_index + 1 < len(self.history):
            self.stop_preview()
            self.history_index += 1
            self.score.notes = copy.deepcopy(self.history[self.history_index])
            self.roll.rebuild()
            self.mark_dirty()

    def change_bpm(self, value):
        if hasattr(self, "roll"):
            self.stop_preview()
            self.score.set_bpm(value)
            self.roll.rebuild()
            self.mark_dirty()

    def change_grid(self):
        self.roll.subdivision = self.grid.currentData()
        self.progress.setSingleStep(round(1000 / self.roll.subdivision))
        self.progress.setPageStep(1000)
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
        image = QImage(path)
        if image.isNull():
            self.host.show_error("图片读取失败")
            return
        try:
            self.embed_cover(image)
        except ValueError as exc:
            self.host.show_error(str(exc))
            return
        self.mark_dirty()

    def embed_cover(self, image):
        self.score.root["cover"] = encode_cover(image)
        self.info_dialog.cover = self.score.root["cover"]
        self.info_dialog.update_cover()

    def fetch_netease(self):
        dialog = NeteaseSongDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selection:
            self.apply_netease(*dialog.selection)

    def apply_netease(self, song, cover):
        if self.info_dialog.apply_netease(song, cover):
            self.info_dialog.accept()

    def save(self, save_as=False):
        self.score.name = self.name.text().strip() or "未命名"
        self.score.author = self.author.text().strip()
        self.score.transcriber = self.transcriber.text().strip()
        if self.draft_mode:
            self.saved, self.dirty = True, False
            self.accept()
            return True
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
        self.update_summary()
        return True

    def preview(self):
        if self.player.state == State.PLAYING:
            self.player.pause()
        elif self.player.state == State.PAUSED:
            self.player.pause(False)
        elif self.host.ensure_audio():
            try:
                self.host.player.stop()
                self.player.load(self.score)
                self.player.set_speed(self.host.player.speed)
                self.player.seek(self.roll.cursor_beat * self.score.ms_per_beat)
                self.player.play(self.host.mixer)
            except ValueError as exc:
                self.host.show_error(str(exc))
        self.update_playback()

    def on_preview_notes(self, keys):
        self.flash_keys(keys)

    def keyPressEvent(self, event):
        if isinstance(self.focusWidget(), (QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox)):
            super().keyPressEvent(event)
            return
        text = event.text().upper()
        bindings = self.host.store.data["keys"]
        if text in bindings and not event.isAutoRepeat():
            key = bindings.index(text)
            self.roll.add_note(key)
            self.audition_key(key)
            event.accept()
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            direction = -1 if event.key() == Qt.Key.Key_Left else 1
            self.move_cursor(direction)
            event.accept()
        elif event.key() == Qt.Key.Key_Space:
            self.preview()
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
        self.preview_timer.stop()
        event.accept()

    def reject(self):
        self.close()
