"""Three-line song records for the cloud library."""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainterPath
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from . import style


class CloudSongDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.column() == 2:
            super().paint(painter, option, index)
            return
        options = QStyleOptionViewItem(option)
        self.initStyleOption(options, index)
        icon = QIcon(options.icon)
        options.text = ""
        options.icon = QIcon()
        options.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, options, painter, options.widget)
        painter.save()
        if index.column() == 0:
            size = min(68, option.rect.width() - 16, option.rect.height() - 16)
            rect = QRect(0, 0, size, size)
            rect.moveCenter(option.rect.center())
            clip = QPainterPath()
            clip.addRoundedRect(rect, 8, 8)
            painter.setClipPath(clip)
            icon.paint(painter, rect)
        else:
            self.paint_details(painter, option, index.data(Qt.ItemDataRole.UserRole) or {})
        painter.restore()

    def paint_details(self, painter, option, item):
        metrics = option.fontMetrics
        height = metrics.height()
        rect = option.rect.adjusted(8, 0, -8, 0)
        top = rect.center().y() - (3 * height + 8) // 2
        alignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        title_font = QFont(option.font)
        title_font.setBold(True)
        title = str(item.get("title") or style.tr("cloud.untitled"))
        painter.setFont(title_font)
        painter.setPen(QColor(style.colors["text"]))
        painter.drawText(
            QRect(rect.left(), top, rect.width(), height), alignment,
            QFontMetrics(title_font).elidedText(title, Qt.TextElideMode.ElideRight, rect.width()),
        )
        painter.setFont(option.font)
        painter.setPen(QColor(style.colors["muted"]))
        artist = str(item.get("artist") or style.tr("cloud.unknownArtist"))
        painter.drawText(
            QRect(rect.left(), top + height + 4, rect.width(), height), alignment,
            metrics.elidedText(artist, Qt.TextElideMode.ElideRight, rect.width()),
        )
        transcriber = str(item.get("transcribed_by") or style.tr("cloud.unknownTranscriber"))
        transcriber = f"{style.tr('cloud.transcriber')} {transcriber}"
        try:
            difficulty = max(0, min(5, int(item.get("difficulty") or 0)))
        except (TypeError, ValueError):
            difficulty = 0
        stars = "★" * difficulty + "☆" * (5 - difficulty)
        star_width = metrics.horizontalAdvance(stars)
        name_width = max(0, rect.width() - star_width - 12)
        name = metrics.elidedText(transcriber, Qt.TextElideMode.ElideRight, name_width)
        bottom = top + 2 * (height + 4)
        painter.drawText(QRect(rect.left(), bottom, name_width, height), alignment, name)
        painter.setPen(QColor(style.colors["accent"]))
        painter.drawText(
            QRect(rect.left() + metrics.horizontalAdvance(name) + 12, bottom, star_width, height),
            alignment, stars,
        )
