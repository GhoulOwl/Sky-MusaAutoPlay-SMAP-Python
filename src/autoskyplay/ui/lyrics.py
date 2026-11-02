"""Compact, song-clock driven lyric display for the score editor."""
from __future__ import annotations

import bisect
import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget


class ScrollingLyrics(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.lines = []
        self.starts = []
        self.message = '暂无歌词'
        self.position_ms = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(2)
        self.previous = self._line('muted')
        self.current = self._line('editorCurrentLyric')
        self.current.setTextFormat(Qt.TextFormat.RichText)
        self.following = self._line('muted')
        for item in (self.previous, self.current, self.following):
            layout.addWidget(item, 1)
        self.setAccessibleName('同步滚动歌词')
        self.set_position(0)

    def _line(self, name):
        item = QLabel(self)
        item.setObjectName(name)
        item.setTextFormat(Qt.TextFormat.PlainText)
        item.setAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return item

    def set_lyrics(self, lyrics, message='暂无歌词'):
        self.lines = lyrics.get('lines', []) if isinstance(lyrics, dict) else []
        self.starts = [line['start'] for line in self.lines]
        self.message = message
        self.set_position(self.position_ms)

    def set_position(self, position_ms):
        self.position_ms = position_ms
        index = bisect.bisect_right(self.starts, position_ms) - 1
        before = self.lines[index - 1]['text'] if index > 0 else ''
        current = self.lines[index] if index >= 0 else None
        following = self.lines[index + 1]['text'] if index + 1 < len(self.lines) else ''
        self.previous.setText(before)
        self.previous.setToolTip(before)
        self.following.setText(following)
        self.following.setToolTip(following)
        if current:
            words = current.get('words', [])
            accent = self.palette().color(QPalette.ColorRole.Highlight).name()
            text = ''.join(f'<span style="color:{accent}">' + html.escape(word['text']) + '</span>'
                           if word['start'] <= position_ms else html.escape(word['text']) for word in words
                           ) if words else html.escape(current['text'])
            self.current.setText(text)
            self.current.setToolTip(current['text'])
        else:
            self.current.setText(html.escape(self.message) if not self.lines else '')
            self.current.setToolTip('')
