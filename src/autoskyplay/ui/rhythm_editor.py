"""Manual beat reference edits in selection-relative seconds."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..transcription.song_analysis import edit_rhythm


class RhythmEditor(QDialog):
    def __init__(self, result, position_ms=0, parent=None):
        super().__init__(parent)
        self.original, self.result = result, None
        self.setWindowTitle('修正共享拍点')
        self.resize(480, 560)
        layout = QVBoxLayout(self)
        explanation = QLabel('时间从当前选段起点计算，单位为秒。双击修改，或添加、删除拍点。\n'
                             '所有声部共用此参考；原始音符保持原时序。小节首拍需另外核实。')
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.table = QTableWidget(0, 1)
        self.table.setHorizontalHeaderLabels(['选段内时间（秒）'])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        self.set_beats(result.analysis.get('beats_ms', []))
        tools = QHBoxLayout()
        self.time = QDoubleSpinBox()
        self.time.setRange(0, result.duration_ms / 1000)
        self.time.setDecimals(3)
        self.time.setValue(position_ms / 1000)
        self.time.setSuffix(' s')
        tools.addWidget(self.time)
        add = QPushButton('添加')
        add.clicked.connect(self.add_beat)
        tools.addWidget(add)
        remove = QPushButton('删除选中')
        remove.clicked.connect(self.remove_beats)
        tools.addWidget(remove)
        layout.addLayout(tools)
        tempo = QHBoxLayout()
        for title, callback in (('半速（隔拍）', self.half), ('倍速（插拍）', self.double),
                                ('恢复原拍点', self.reset)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            tempo.addWidget(button)
        layout.addLayout(tempo)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def beats(self):
        return [float(self.table.item(i, 0).text()) * 1000 for i in range(self.table.rowCount())]

    def set_beats(self, beats):
        self.table.setRowCount(len(beats))
        for i, value in enumerate(beats):
            self.table.setItem(i, 0, QTableWidgetItem(f'{value / 1000:.6f}'))

    def transform(self, operation):
        try:
            self.set_beats(operation(self.beats()))
            self.error.clear()
        except ValueError:
            self.error.setText('请先修正表中的无效数字。')

    def add_beat(self):
        self.transform(lambda beats: sorted([*beats, self.time.value() * 1000]))

    def remove_beats(self):
        for row in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)

    def half(self):
        self.transform(lambda beats: beats[::2])

    def double(self):
        self.transform(lambda beats: [v for a, b in zip(beats[:-1], beats[1:]) for v in (a, (a + b) / 2)] + beats[-1:])

    def reset(self):
        self.set_beats(self.original.analysis.get('rhythm_original', self.original.analysis).get('beats_ms', []))
        self.error.clear()

    def apply(self):
        try:
            self.result = edit_rhythm(self.original, self.beats())
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.accept()
