from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..input import DEFAULT_KEYS, validate_keys
from ..midi import MidiImporter
from . import style
from .widgets import button, label


class SettingsDialog(QDialog):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.setWindowTitle(style.tr("settings"))
        self.resize(650, 650)
        data = host.store.data
        root = QVBoxLayout(self)
        root.addWidget(label(style.tr("settings"), "heading"))
        form = QFormLayout()
        self.language = QComboBox()
        for key, name in style.LANGUAGES.items():
            self.language.addItem(name, key)
        self.language.setCurrentIndex(list(style.LANGUAGES).index(data["language"]))
        form.addRow(style.tr("set.lang"), self.language)
        self.theme = QComboBox()
        for key in style.THEMES:
            self.theme.addItem(style.tr("theme." + key), key)
        self.theme.setCurrentIndex(list(style.THEMES).index(data["theme"]))
        form.addRow(style.tr("theme"), self.theme)
        self.font = QSpinBox()
        self.font.setRange(10, 22)
        self.font.setValue(data["font_size"])
        form.addRow(style.tr("set.fontScale"), self.font)
        self.countdown = QDoubleSpinBox()
        self.countdown.setRange(0, 30)
        self.countdown.setValue(data["countdown"])
        self.countdown.setSuffix(" s")
        form.addRow(style.tr("set.wait"), self.countdown)
        self.hold = QSpinBox()
        self.hold.setRange(10, 500)
        self.hold.setValue(data["hold_ms"])
        self.hold.setSuffix(" ms")
        form.addRow("游戏按键保持时长", self.hold)
        self.sustain_keys = QCheckBox("持续音色按谱面音长保持按键（实验）")
        self.sustain_keys.setChecked(data.get("sustain_keys", False))
        self.sustain_keys.setEnabled(sys.platform == "win32")
        self.sustain_keys.setToolTip("仅作用于持续型音色；需在游戏中确认乐器支持长按发声。")
        form.addRow("游戏长音", self.sustain_keys)
        self.global_keys = QCheckBox("F1–F6 / ← / →")
        self.global_keys.setChecked(data["global_hotkeys"])
        self.global_keys.setEnabled(sys.platform == "win32")
        form.addRow("全局快捷键 (Windows)", self.global_keys)
        self.random_speed = QCheckBox(style.tr("speed.random"))
        self.random_speed.setChecked(data["random_speed"])
        form.addRow(style.tr("speed"), self.random_speed)
        self.cloud = QLineEdit(data["cloud_url"])
        form.addRow("云端地址", self.cloud)
        self.netease_port = QSpinBox()
        self.netease_port.setRange(1, 65535)
        self.netease_port.setValue(data["netease_port"])
        form.addRow("网易云服务端口", self.netease_port)
        self.repo = QLineEdit(data["update_repo"])
        self.repo.setPlaceholderText("owner/python-rewrite-repo")
        form.addRow("Python 版更新仓库", self.repo)
        root.addLayout(form)
        mapping = QGroupBox(style.tr("keys.header"))
        grid = QGridLayout(mapping)
        self.keys = []
        for i, key in enumerate(data["keys"]):
            edit = QLineEdit(key)
            edit.setMaxLength(5)
            edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(edit, i // 5, i % 5)
            self.keys.append(edit)
        root.addWidget(mapping)
        row = QHBoxLayout()
        row.addWidget(button("set.pitchReset", lambda: self.reset_pitch()))
        row.addWidget(button("keys.reset", self.reset_keys))
        row.addStretch()
        root.addLayout(row)
        self.clear_audio_button = QPushButton("清理扒谱音频缓存（下载文件保留 7 天）")
        self.clear_audio_button.clicked.connect(self.clear_audio_cache)
        root.addWidget(self.clear_audio_button)
        models = QPushButton('扒谱模型管理（MuScriptor small / medium）')
        models.clicked.connect(self.manage_models)
        root.addWidget(models)
        clear_engine = QPushButton("清理本地扒谱运行组件")
        clear_engine.clicked.connect(self.clear_transcription)
        root.addWidget(clear_engine)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.reset_pitches = False

    def manage_models(self):
        from .model_manager import ModelManager
        ModelManager(self.host, self).exec()

    def clear_audio_cache(self):
        from ..transcription.cache import DownloadCache
        from ..transcription.drafts import DraftStore

        dialog = self.host.transcription_dialog
        if dialog and dialog.active and dialog.busy:
            QMessageBox.warning(self, '清理音频缓存', '请等待当前音频准备或扒谱任务结束')
            return
        if QMessageBox.question(self, '清理音频缓存',
                                '清理下载缓存和临时试听音频？草稿源文件、曲谱和人工编辑均会保留。',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            if dialog and dialog.active:
                dialog.clear_audio_cache(confirm=False)
            else:
                DownloadCache(self.host.store.directory).prune(clear=True)
                DraftStore(self.host.store.directory).prune_audio(budget=0)
                from ..transcription.hybrid import clear_hybrid_audio
                clear_hybrid_audio(self.host.store.directory)
        except OSError as exc:
            QMessageBox.warning(self, '清理音频缓存', str(exc))

    def clear_transcription(self):
        from ..transcription.engine import clear_components

        dialog = self.host.transcription_dialog
        if dialog and dialog.active and dialog.busy:
            QMessageBox.warning(self, "扒谱组件", "请先取消并等待当前任务或组件安装结束")
            return
        if QMessageBox.question(self, "清理扒谱组件", "移除已安装的运行组件？MuScriptor／GAME 权重、草稿和曲谱会保留。",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            clear_components(self.host.store.directory)
            if dialog and dialog.active:
                dialog.update_engine()

    def reset_pitch(self):
        self.reset_pitches = True

    def reset_keys(self):
        for edit, key in zip(self.keys, DEFAULT_KEYS):
            edit.setText(key)

    def save(self):
        keys = [e.text().strip().upper() for e in self.keys]
        try:
            validate_keys(keys)
            if not self.cloud.text().startswith(("https://", "http://")):
                raise ValueError("云端地址需要以 https:// 或 http:// 开头")
            self.host.netease_service.configure(self.netease_port.value())
        except ValueError as exc:
            QMessageBox.warning(self, style.tr("settings"), str(exc))
            return
        self.host.store.data.update(
            language=self.language.currentData(),
            theme=self.theme.currentData(),
            font_size=self.font.value(),
            countdown=self.countdown.value(),
            hold_ms=self.hold.value(),
            sustain_keys=self.sustain_keys.isChecked(),
            keys=keys,
            cloud_url=self.cloud.text().rstrip("/"),
            update_repo=self.repo.text().strip(),
            global_hotkeys=self.global_keys.isChecked(),
            random_speed=self.random_speed.isChecked(),
            netease_port=self.netease_port.value(),
        )
        if self.reset_pitches:
            self.host.store.data["pitches"] = {}
        self.accept()


class MidiDialog(QDialog):
    def __init__(self, host, path):
        super().__init__(host)
        self.importer = MidiImporter(path)
        self.score = None
        self.setWindowTitle("MIDI · " + path.name)
        self.resize(500, 420)
        root = QVBoxLayout(self)
        root.addWidget(label(f"BPM {self.importer.bpm:g} · {path.name}", "heading"))
        self.tracks = QListWidget()
        for index, name, count in self.importer.tracks():
            item = QListWidgetItem(f"{name} · {count} notes")
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if count else Qt.CheckState.Unchecked)
            self.tracks.addItem(item)
        root.addWidget(self.tracks)
        self.shift = QSpinBox()
        self.shift.setRange(-60, 60)
        self.shift.setValue(self.importer.suggest_shift(self.selected_tracks()))
        form = QFormLayout()
        form.addRow("移调（半音，包含八度）", self.shift)
        root.addLayout(form)
        root.addWidget(
            button(
                "midi.auto", lambda: self.shift.setValue(self.importer.suggest_shift(self.selected_tracks()))
            )
        )
        self.drums = QCheckBox("包括打击乐音轨")
        root.addWidget(self.drums)
        root.addWidget(
            label("保留所有音轨的变速事件；超出 15 键音域的音符折回八度，半音按旋律方向贴近白键。", "muted")
        )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.convert)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def selected_tracks(self):
        return {
            self.tracks.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.tracks.count())
            if self.tracks.item(i).checkState() == Qt.CheckState.Checked
        }

    def convert(self):
        try:
            self.score = self.importer.convert(
                self.selected_tracks(), self.shift.value(), self.drums.isChecked()
            )
        except ValueError as exc:
            QMessageBox.warning(self, "MIDI", str(exc))
            return
        self.accept()


class LoginDialog(QDialog):
    def __init__(self, host):
        super().__init__(host)
        self.setWindowTitle(style.tr("login"))
        root = QVBoxLayout(self)
        root.addWidget(label("缪斯树屋", "heading"))
        form = QFormLayout()
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(style.tr("login.user"), self.username)
        form.addRow(style.tr("login.pass"), self.password)
        root.addLayout(form)
        self.remember = QCheckBox(style.tr("remember"))
        root.addWidget(self.remember)
        root.addWidget(label(style.tr("rememberhint"), "muted"))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
