"""Explicit, cancellable ModelScope model management on a background thread."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..transcription.game_models import GameModelStore
from ..transcription.models import ModelStore


class ModelManager(QDialog):
    progressed = Signal(int)
    finished = Signal(str)

    def __init__(self, host, parent=None):
        super().__init__(parent or host)
        self.host = host
        self.store = ModelStore(host.store.directory)
        self.cancelled = threading.Event()
        self.running = False
        self.close_pending = False
        self.setWindowTitle('扒谱模型管理')
        self.resize(570, 330)
        root = QVBoxLayout(self)
        intro = QLabel('本地扒谱模型\nMuScriptor 从 ModelScope 社区镜像下载；GAME 从作者 GitHub 发布页下载，校验后启用。\n'
                       '模型长期保存；清理歌曲缓存不会删除模型，删除模型也不会影响草稿。')
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.choice = QComboBox()
        self.choice.addItem('small · 默认 · 约 412 MB', 'small')
        self.choice.addItem('medium · 可选 · 约 1.23 GB', 'medium')
        self.choice.addItem('GAME medium · 混合人声 · 约 200 MB', 'game-medium')
        self.choice.currentIndexChanged.connect(self.refresh)
        root.addWidget(self.choice)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        root.addWidget(self.progress)
        row = QHBoxLayout()
        self.download = QPushButton('下载 / 继续下载')
        self.download.clicked.connect(lambda: self.start())
        self.local = QPushButton('导入本地模型')
        self.local.clicked.connect(self.import_local)
        self.remove = QPushButton('删除所选模型')
        self.remove.clicked.connect(self.delete)
        for button in (self.download, self.local, self.remove):
            row.addWidget(button)
        root.addLayout(row)
        self.cancel = QPushButton('取消下载')
        self.cancel.clicked.connect(self.cancelled.set)
        self.cancel.hide()
        root.addWidget(self.cancel)
        self.license_label = QLabel()
        self.license_label.setWordWrap(True)
        root.addWidget(self.license_label)
        self.progressed.connect(self.progress.setValue)
        self.finished.connect(self.complete)
        self.refresh()

    def refresh(self, *_):
        game = self.choice.currentData() == 'game-medium'
        self.store = (GameModelStore if game else ModelStore)(self.host.store.directory)
        size = 'medium' if game else self.choice.currentData()
        self.license_label.setText('GAME 权重：CC BY-NC-SA 4.0 · 仅非商业使用\n'
            '本地导入目录需包含 model.pt、config.yaml、lang_map.json。' if game else
            'MuScriptor 权重：CC BY-NC 4.0 · 仅非商业使用\n本地导入目录需包含 model.safetensors、config.json。')
        installed = self.store.installed(size)
        self.status.setText(('已安装，可离线使用' if installed else '尚未安装，或文件需要重新校验')
                            + '\n版本：' + self.store.catalog[size]['revision'][:8])
        self.download.setText('校验已安装模型' if installed else '下载 / 继续下载')

    def allowed(self):
        dialog = getattr(self.host, 'transcription_dialog', None)
        if dialog and dialog.busy:
            QMessageBox.information(self, '模型管理', '请等待当前扒谱或音频准备任务结束')
            return False
        return not self.running

    def import_local(self):
        if self.allowed():
            folder = QFileDialog.getExistingDirectory(self, '选择模型文件夹')
            if folder:
                self.start(Path(folder))

    def start(self, source=None):
        if not self.allowed():
            return
        self.cancelled.clear()
        self.running = True
        size = 'medium' if self.choice.currentData() == 'game-medium' else self.choice.currentData()
        for widget in (self.choice, self.download, self.local, self.remove):
            widget.setEnabled(False)
        self.progress.setValue(0)
        self.progress.show()
        self.cancel.show()
        self.status.setText('导入并校验模型…' if source else '连接模型来源，下载并校验模型…')

        def work():
            try:
                self.store.install(size, source=source, cancelled=self.cancelled.is_set,
                                   progress=self.progressed.emit)
                message = '模型已安装并通过 SHA256 校验'
            except Exception as exc:
                message = str(exc)
            self.finished.emit(message)

        self.thread = threading.Thread(target=work, name='muscriptor-model-download', daemon=True)
        self.thread.start()

    def complete(self, message):
        self.running = False
        for widget in (self.choice, self.download, self.local, self.remove):
            widget.setEnabled(True)
        self.cancel.hide()
        self.refresh()
        self.status.setText(message)
        dialog = getattr(self.host, 'transcription_dialog', None)
        if dialog:
            dialog.update_engine()
        if self.close_pending:
            self.close()

    def delete(self):
        if not self.allowed():
            return
        size = 'medium' if self.choice.currentData() == 'game-medium' else self.choice.currentData()
        if QMessageBox.question(self, '删除模型', f'删除 {size} 模型及未完成的下载？草稿和源音频会保留。') != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete(size)
            self.refresh()
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))

    def reject(self):
        self.close()

    def closeEvent(self, event):
        if self.running:
            self.cancelled.set()
            self.close_pending = True
            self.status.setText('正在取消并保留下载进度…')
            event.ignore()
        else:
            event.accept()
