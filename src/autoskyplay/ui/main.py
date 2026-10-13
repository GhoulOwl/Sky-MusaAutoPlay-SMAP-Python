from __future__ import annotations

import copy
import logging
import random
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..audio import ASSETS, DEFAULT_PITCH, INSTRUMENTS, Mixer, QtAudioOutput
from ..auth import clear_auth, load_auth, save_auth
from ..cloud import CloudClient, check_release
from ..input import GlobalKeyboard, WindowsOutput
from ..playback import Player, State
from ..score import Score, load_score
from ..storage import scan_library
from ..updater import download_asset, installer_asset
from . import style
from .dialogs import LoginDialog, MidiDialog, SettingsDialog
from .editor import EditorDialog
from .practice import PracticeDialog
from .widgets import Job, Piano, VolumeButton, button, cover_icon, label, playback_icon, time_text

log = logging.getLogger(__name__)
PLAYBACK_ORDERS = (
    ("sequence", "sequence"), ("list", "listloop"), ("shuffle", "shuffle"), ("single", "singleloop"),
)
PITCH_NAMES = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")
PITCH_VALUES = (*range(12, -1, -1), -12, 24)


def pitch_text(value):
    return f"{value:+d} {PITCH_NAMES[value % 12]}" if value else "0 C"


class Bridge(QObject):
    notes = Signal(object)
    done = Signal()
    error = Signal(str)
    key = Signal(str, bool)


class MainWindow(QMainWindow):
    def __init__(self, store, no_audio=False, no_global=False):
        super().__init__()
        self.store, self.no_audio, self.no_global = store, no_audio, no_global
        self.bridge = Bridge(self)
        self.player = Player(self.bridge.notes.emit, self.bridge.done.emit, self.bridge.error.emit)
        self.mixer = Mixer()
        self.audio_output = None
        self.audio_prepared = None
        self.current: Score | None = None
        self.scores = []
        self.visible_scores = []
        self.queue: list[Score] = []
        self.jobs = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.closed = False
        self.practice_dialog = None
        self.cloud_client = CloudClient(store.data["cloud_url"])
        self.cloud_page = 0
        self.cloud_pages = 0
        self.cloud_items = []
        self.cloud_generation = 0
        self.cloud_loading = False
        self.global_keyboard = GlobalKeyboard(self.bridge.key.emit)
        self.pressed = set()
        self.manual_active = set()
        self.autoplay = QTimer(self)
        self.autoplay.setSingleShot(True)
        self.autoplay.timeout.connect(lambda: self.next_song(1, True, automatic=True))
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.save_state)
        self.bridge.notes.connect(self.notes_fired)
        self.bridge.done.connect(self.finished_song)
        self.bridge.error.connect(self.show_error)
        self.bridge.key.connect(self.global_key)
        style.current_language = store.data.get("language", "zh_CN")
        style.apply_theme(QApplication.instance(), store.data["theme"], store.data["font_size"])
        self.setWindowTitle("AutoSkyPlay · Python")
        self.setWindowIcon(QIcon(str(ASSETS / "app.ico")))
        self.resize(1280, 820)
        self.setMinimumSize(1060, 670)
        self.build()
        self.restore()
        self.player.set_speed(store.data["speed"])
        self.player.random_speed = store.data["random_speed"]
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(40)
        QApplication.instance().installEventFilter(self)
        self.start_global_keyboard()
        if self.store.data.get("remember_login") and not no_global:
            self.run_job(
                lambda: load_auth(self.cloud_client.base_url),
                self.restore_auth,
                on_error=lambda message: self.status.setText(message),
            )
        if self.store.data["update_repo"] and not no_global:
            QTimer.singleShot(3000, self.check_updates)

    def panel(self):
        panel = QFrame()
        panel.setObjectName("panel")
        return panel

    def build(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 18, 16, 12)
        root.setSpacing(14)
        self.setCentralWidget(central)
        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)
        sidebar = QWidget()
        sidebar.setFixedWidth(190)
        left = QVBoxLayout(sidebar)
        left.setContentsMargins(0, 4, 0, 0)
        brand = label("AutoSkyPlay", "brand")
        left.addWidget(brand)
        left.addWidget(label("SKY · MUSIC STUDIO", "muted"))
        self.account = button("login", self.login)
        left.addSpacing(12)
        left.addWidget(self.account)
        left.addSpacing(16)
        self.local_nav = button("library", lambda: self.show_page(0))
        self.cloud_nav = button("cloud", lambda: self.show_page(1))
        left.addWidget(self.local_nav)
        left.addWidget(self.cloud_nav)
        left.addSpacing(18)
        row = QHBoxLayout()
        row.addWidget(label(style.tr("favorites"), "muted"))
        add = QPushButton("+")
        add.setFixedWidth(34)
        add.clicked.connect(self.create_collection)
        row.addWidget(add)
        left.addLayout(row)
        self.collections = QListWidget()
        self.collections.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.collections.currentRowChanged.connect(self.collection_changed)
        self.collections.model().rowsMoved.connect(self.reorder_collections)
        self.collections.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.collections.customContextMenuRequested.connect(self.collection_menu)
        left.addWidget(self.collections, 1)
        left.addWidget(button("settings", self.settings))
        left.addWidget(button("about", self.about))
        body.addWidget(sidebar)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        body.addWidget(split, 1)
        center = QWidget()
        content = QVBoxLayout(center)
        content.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        self.heading = label(style.tr("library"), "heading")
        header.addWidget(self.heading)
        header.addStretch()
        header.addWidget(button("import", self.import_files))
        header.addWidget(button("create", lambda: self.edit_score(Score()), accent=True))
        content.addLayout(header)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(style.tr("search"))
        self.search.textChanged.connect(self.filter_library)
        self.search.returnPressed.connect(
            lambda: self.load_cloud(True) if self.pages.currentIndex() else None
        )
        filters.addWidget(self.search, 1)
        self.sort = QComboBox()
        self.sort.addItems([style.tr("sort.az"), style.tr("sort.za"), style.tr("sort.fav")])
        self.sort.currentIndexChanged.connect(self.filter_library)
        filters.addWidget(self.sort)
        filters.addWidget(button("refresh", self.refresh))
        content.addLayout(filters)
        self.pages = QStackedWidget()
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["☆", style.tr("title"), style.tr("artist"), style.tr("transcriber"), style.tr("duration")]
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column, width in [(0, 38), (2, 100), (3, 100), (4, 65)]:
            self.table.setColumnWidth(column, width)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setIconSize(QSize(38, 38))
        self.table.cellDoubleClicked.connect(self.open_row)
        self.table.cellClicked.connect(self.click_row)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.song_menu)
        self.pages.addWidget(self.table)
        self.build_cloud()
        content.addWidget(self.pages, 1)
        self.library_status = label("", "muted")
        content.addWidget(self.library_status)
        split.addWidget(center)
        self.right = self.panel()
        self.right.setMinimumWidth(295)
        right = QVBoxLayout(self.right)
        right.setContentsMargins(15, 18, 15, 14)
        row = QHBoxLayout()
        row.addWidget(label("♪  " + style.tr("instrument"), "brand"))
        row.addStretch()
        row.addWidget(button("practice", self.practice))
        right.addLayout(row)
        right.addWidget(label("15 KEYS · PLAY SOMETHING", "muted"))
        self.piano = Piano(self.store.data["keys"])
        self.piano.pressed.connect(self.manual_press)
        self.piano.released.connect(self.manual_release)
        right.addWidget(self.piano)
        self.instrument = QComboBox()
        for name in INSTRUMENTS:
            self.instrument.addItem(style.tr(name), name)
        self.instrument.setCurrentIndex(max(0, self.instrument.findData(self.store.data["instrument"])))
        self.instrument.currentIndexChanged.connect(
            lambda: self.instrument_changed(self.instrument.currentData())
        )
        right.addWidget(self.instrument)
        row = QHBoxLayout()
        self.cave = QCheckBox(style.tr("cave"))
        self.cave.setChecked(self.store.data["cave"])
        self.cave.toggled.connect(self.cave_changed)
        row.addWidget(self.cave)
        right.addLayout(row)
        right.addStretch(1)
        split.addWidget(self.right)
        split.setSizes([680, 335])
        self.build_player(root)
        self.build_queue(central)
        self.status = label(style.tr("ready"), "muted")
        root.addWidget(self.status)
        self.populate_collections()

    def build_queue(self, central):
        self.queue_panel = self.panel()
        self.queue_panel.setParent(central)
        self.queue_panel.setAccessibleName(style.tr("tip.playlist"))
        layout = QVBoxLayout(self.queue_panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        header = QHBoxLayout()
        self.queue_heading = label(style.tr("tip.playlist"), "brand")
        header.addWidget(self.queue_heading, 1)
        close = QPushButton("×")
        close.setObjectName("transport")
        close.setFixedSize(44, 44)
        close.setToolTip(style.tr("hidequeue"))
        close.setAccessibleName(style.tr("hidequeue"))
        close.clicked.connect(lambda: self.queue_button.setChecked(False))
        header.addWidget(close)
        layout.addLayout(header)
        self.queue_list = QListWidget()
        self.queue_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.queue_list.model().rowsMoved.connect(self.reorder_queue)
        self.queue_list.itemDoubleClicked.connect(self.queue_play)
        self.queue_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue_list.customContextMenuRequested.connect(self.queue_menu)
        layout.addWidget(self.queue_list, 1)
        self.queue_empty = label(style.tr("queueempty"), "muted")
        self.queue_empty.setWordWrap(True)
        layout.addWidget(self.queue_empty)
        footer = QHBoxLayout()
        footer.addStretch()
        self.clear_queue_button = button("clearqueue", self.clear_queue)
        footer.addWidget(self.clear_queue_button)
        layout.addLayout(footer)
        self.queue_panel.hide()

    def toggle_queue(self, visible):
        self.queue_button.setIcon(playback_icon("queue", visible))
        self.queue_button.setToolTip(style.tr("hidequeue" if visible else "showqueue"))
        self.queue_button.setAccessibleName(self.queue_button.toolTip())
        self.queue_panel.setVisible(visible)
        if visible:
            self.position_queue()
            self.queue_panel.raise_()
        else:
            self.queue_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def position_queue(self):
        if not hasattr(self, "queue_panel") or self.queue_panel.isHidden():
            return
        width = max(360, self.queue_panel.minimumSizeHint().width())
        height = min(360, self.player_panel.y() - 32)
        self.queue_panel.setGeometry(
            self.centralWidget().width() - width - 16,
            self.player_panel.y() - height - 12, width, height,
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.position_queue()
        if hasattr(self, "volume_button") and self.volume_button.popup.isVisible():
            self.volume_button.position_popup()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            if hasattr(self, "loop_button"):
                self.update_playback_order()
            if hasattr(self, "queue_button"):
                self.queue_button.setIcon(playback_icon("queue", self.queue_button.isChecked()))

    def cycle_playback_order(self):
        values = [value for value, _ in PLAYBACK_ORDERS]
        self.playback_order = values[(values.index(self.playback_order) + 1) % len(values)]
        self.update_playback_order()
        self.defer_save()

    def update_playback_order(self):
        key = dict(PLAYBACK_ORDERS)[self.playback_order]
        self.loop_button.setIcon(playback_icon(self.playback_order))
        self.loop_button.setToolTip(style.tr(key) + " · " + style.tr("cyclemode"))
        self.loop_button.setAccessibleName(style.tr("tip.playmode") + ": " + style.tr(key))

    def output_mode_changed(self, _mode, checked):
        if checked:
            self.stop()

    def build_player(self, root):
        panel = self.player_panel = self.panel()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 9, 16, 12)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 10000)
        self.seek.sliderMoved.connect(self.seek_song)
        seek_row = QHBoxLayout()
        seek_row.addWidget(self.seek, 1)
        self.time = label("0:00 / 0:00", "muted")
        seek_row.addWidget(self.time)
        layout.addLayout(seek_row)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.now_cover = QLabel("♪")
        self.now_cover.setFixedSize(48, 48)
        row.addWidget(self.now_cover)
        info = QVBoxLayout()
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.now_title = label(style.tr("nosong"))
        self.now_title.setMinimumWidth(70)
        self.now_title.setMaximumWidth(260)
        self.now_title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.now_meta = label("", "muted")
        self.now_meta.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_row.addWidget(self.now_title)
        self.star = QPushButton("☆")
        self.star.setObjectName("favorite")
        self.star.setFixedSize(44, 44)
        self.star.setEnabled(False)
        self.star.setToolTip(style.tr("favorite"))
        self.star.setAccessibleName(style.tr("favorite"))
        self.star.clicked.connect(lambda: self.favorite(self.current) if self.current else None)
        title_row.addWidget(self.star)
        title_row.addStretch()
        info.addLayout(title_row)
        info.addWidget(self.now_meta)
        row.addLayout(info, 1)

        def control(text, tooltip, callback):
            widget = QPushButton(text)
            widget.setObjectName("transport")
            widget.setFixedSize(44, 44)
            widget.setToolTip(tooltip)
            widget.setAccessibleName(tooltip)
            if callback:
                widget.clicked.connect(callback)
            row.addWidget(widget)
            return widget

        self.playback_order = self.store.data["loop"]
        self.loop_button = control("", "", self.cycle_playback_order)
        self.loop_button.setIconSize(QSize(24, 24))
        self.update_playback_order()
        self.previous_button = control(
            "|◀", style.tr("previous") + " · F5", lambda: self.next_song(-1, True),
        )
        self.play_button = control(
            "▶", style.tr("play") + " / " + style.tr("pause") + " · F2", self.toggle_play,
        )
        self.play_button.setObjectName("accent")
        self.next_button = control("▶|", style.tr("next") + " · F6", lambda: self.next_song(1, True))
        self.stop_button = control("■", style.tr("stop"), self.stop)
        self.queue_button = control("", style.tr("showqueue"), None)
        self.queue_button.setIcon(playback_icon("queue"))
        self.queue_button.setIconSize(QSize(24, 24))
        self.queue_button.setCheckable(True)
        self.queue_button.toggled.connect(self.toggle_queue)
        mode_switch = QFrame()
        mode_switch.setObjectName("modeSwitch")
        modes = QHBoxLayout(mode_switch)
        modes.setContentsMargins(2, 0, 2, 0)
        modes.setSpacing(0)
        self.mode = QButtonGroup(panel)
        self.preview_button = button("preview")
        self.perform_button = button("perform")
        for index, widget in enumerate((self.preview_button, self.perform_button)):
            widget.setObjectName("modeOption")
            widget.setCheckable(True)
            widget.setMinimumHeight(44)
            self.mode.addButton(widget, index)
            if index:
                modes.addWidget(label("｜", "muted"))
            modes.addWidget(widget)
        self.preview_button.setChecked(True)
        self.preview_button.setToolTip(style.tr("tip.preview"))
        self.perform_button.setToolTip(style.tr("tip.perform"))
        if sys.platform != "win32":
            self.perform_button.setEnabled(False)
            self.perform_button.setToolTip(style.tr("perform.windows"))
        self.mode.idToggled.connect(self.output_mode_changed)
        row.addWidget(mode_switch)
        row.addWidget(label(style.tr("pitch"), "muted"))
        self.pitch = QComboBox()
        self.pitch.setMaxVisibleItems(len(PITCH_VALUES))
        self.pitch.setAccessibleName(style.tr("pitch"))
        self.pitch.setToolTip(style.tr("pitch"))
        for value in PITCH_VALUES:
            self.pitch.addItem(pitch_text(value), value)
        name = self.instrument.currentData()
        self.set_pitch_selection(self.store.data["pitches"].get(name, DEFAULT_PITCH.get(name, 0)))
        self.pitch.currentIndexChanged.connect(lambda: self.pitch_changed(self.pitch.currentData()))
        row.addWidget(self.pitch)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.1, 3)
        self.speed.setSingleStep(0.1)
        self.speed.setDecimals(1)
        self.speed.setSuffix("×")
        self.speed.setValue(self.store.data["speed"])
        self.speed.valueChanged.connect(self.speed_changed)
        row.addWidget(self.speed)
        self.volume_button = VolumeButton(int(self.store.data["volume"] * 100), self.centralWidget())
        self.volume_button.setIconSize(QSize(24, 24))
        self.volume = self.volume_button.slider
        self.volume.valueChanged.connect(self.volume_changed)
        row.addWidget(self.volume_button)
        layout.addLayout(row)
        root.addWidget(panel)

    def build_cloud(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.cloud_sort = QComboBox()
        for text, value in [("cloud.newest", "newest"), ("cloud.hot", "hot"), ("cloud.downs", "downloads")]:
            self.cloud_sort.addItem(style.tr(text), value)
        self.cloud_sort.currentIndexChanged.connect(lambda: self.load_cloud(True))
        row.addWidget(self.cloud_sort)
        self.difficulty = QComboBox()
        self.difficulty.addItem(style.tr("filter.all"), 0)
        for i in range(1, 6):
            self.difficulty.addItem("★" * i, i)
        self.difficulty.currentIndexChanged.connect(lambda: self.load_cloud(True))
        row.addWidget(self.difficulty)
        row.addStretch()
        self.download_button = button("download", self.download_cloud, accent=True)
        row.addWidget(self.download_button)
        layout.addLayout(row)
        self.cloud_table = QTableWidget(0, 4)
        self.cloud_table.setHorizontalHeaderLabels([style.tr("title"), style.tr("artist"), "★", "↓"])
        self.cloud_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.cloud_table.verticalHeader().hide()
        self.cloud_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cloud_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cloud_table.cellDoubleClicked.connect(lambda *_: self.download_cloud())
        self.cloud_table.verticalScrollBar().valueChanged.connect(self.cloud_scroll)
        layout.addWidget(self.cloud_table, 1)
        self.more_button = button("more", lambda: self.load_cloud(False))
        layout.addWidget(self.more_button)
        self.pages.addWidget(panel)

    def run_job(self, callback, on_result=None, on_error=None, on_finished=None):
        job = Job(callback)
        self.jobs.add(job)
        if on_result:

            def result(value):
                if not self.closed:
                    try:
                        on_result(value)
                    except Exception as exc:
                        self.show_error(str(exc))

            job.signals.result.connect(result)
        job.signals.error.connect(
            lambda message: (on_error or self.show_error)(message) if not self.closed else None
        )

        def finished():
            self.jobs.discard(job)
            if on_finished and not self.closed:
                on_finished()

        job.signals.finished.connect(finished)
        self.pool.start(job)

    def restore(self):
        result = scan_library(self.store.songs_dir)
        self.scores = result.scores
        by_path = {str(s.path): s for s in self.scores}
        self.queue = [by_path[path] for path in self.store.data["queue"] if path in by_path]
        current = by_path.get(self.store.data["current"])
        if current:
            self.select_score(current)
            self.player.seek(float(self.store.data["position_ms"]))
        self.update_queue()
        self.filter_library()
        self.scan_errors = result.errors
        if result.errors:
            self.status.setText(f"{len(result.errors)} 个曲谱读取失败；详情写入日志")
            for path, error in result.errors:
                log.warning("%s: %s", path.name, error)

    def refresh(self):
        if self.pages.currentIndex() == 1:
            self.load_cloud(True)
            return
        self.run_job(lambda: scan_library(self.store.songs_dir), self.library_loaded)

    def library_loaded(self, result):
        self.scores = result.scores
        self.scan_errors = result.errors
        self.filter_library()
        if result.errors:
            self.status.setText(f"{len(result.errors)} 个曲谱读取失败；详情写入日志")
            for path, error in result.errors:
                log.warning("%s: %s", path.name, error)

    def filter_library(self):
        if not hasattr(self, "pages"):
            return
        query = self.search.text().strip().casefold()
        collection_item = self.collections.currentItem()
        files = collection_item.data(Qt.ItemDataRole.UserRole)["files"] if collection_item else None
        scores = [
            s
            for s in self.scores
            if (files is None or str(s.path) in files)
            and query in " ".join([s.name, s.author, s.transcriber]).casefold()
        ]
        sort_index = self.sort.currentIndex()
        scores.sort(
            key=lambda s: (
                (not self.store.is_favorite(s.path)) if sort_index == 2 else False,
                s.name.casefold(),
            ),
            reverse=sort_index == 1,
        )
        self.visible_scores = scores
        self.table.setRowCount(len(scores))
        for row, score in enumerate(scores):
            texts = [
                "★" if self.store.is_favorite(score.path) else "☆",
                score.name,
                score.author or "—",
                score.transcriber or "—",
                time_text(score.duration_ms),
            ]
            for column, text in enumerate(texts):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if column == 1:
                    item.setIcon(cover_icon(score))
                self.table.setItem(row, column, item)
            self.table.setRowHeight(row, 58)
        if self.pages.currentIndex() == 0:
            self.library_status.setText(
                f"{len(scores)} 首曲谱 · 双击播放 · 右键更多" if scores else style.tr("empty")
            )

    def show_page(self, page):
        if page == 0:
            self.collections.setCurrentRow(-1)
        self.pages.setCurrentIndex(page)
        self.heading.setText(style.tr("library" if page == 0 else "cloud"))
        self.sort.setVisible(page == 0)
        if page == 1 and not self.cloud_items:
            self.load_cloud(True)
        else:
            self.filter_library()

    def populate_collections(self):
        self.collections.blockSignals(True)
        self.collections.clear()
        for collection in self.store.data["collections"]:
            item = QListWidgetItem(f"☆  {collection['name']}  ({len(collection['files'])})")
            item.setData(Qt.ItemDataRole.UserRole, copy.deepcopy(collection))
            self.collections.addItem(item)
        self.collections.blockSignals(False)

    def collection_changed(self, row):
        if row >= 0:
            self.pages.setCurrentIndex(0)
            self.heading.setText(self.store.data["collections"][row]["name"])
            self.sort.show()
        self.filter_library()

    def create_collection(self):
        name, ok = QInputDialog.getText(self, style.tr("newcollection"), style.tr("title"))
        if ok and name.strip():
            self.store.data["collections"].append({"name": name.strip(), "files": []})
            self.populate_collections()
            self.defer_save()

    def reorder_collections(self, *_):
        self.store.data["collections"] = [
            self.collections.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.collections.count())
        ]
        self.defer_save()

    def collection_menu(self, point):
        item = self.collections.itemAt(point)
        if item is None:
            return
        row = self.collections.row(item)
        menu = QMenu(self)
        rename = menu.addAction(style.tr("rename"))
        delete = menu.addAction(style.tr("delete"))
        choice = menu.exec(self.collections.mapToGlobal(point))
        if choice == rename:
            name, ok = QInputDialog.getText(
                self, style.tr("rename"), style.tr("title"), text=self.store.data["collections"][row]["name"]
            )
            if ok and name.strip():
                self.store.data["collections"][row]["name"] = name.strip()
        elif choice == delete:
            self.store.data["collections"].pop(row)
        else:
            return
        self.populate_collections()
        self.filter_library()
        self.defer_save()

    def favorite(self, score, collection=0):
        if score:
            selected = self.collections.currentRow()
            self.store.toggle_favorite(score.path, collection)
            self.populate_collections()
            self.collections.setCurrentRow(selected)
            self.filter_library()
            self.update_now_playing()

    def click_row(self, row, column):
        if column == 0:
            self.favorite(self.visible_scores[row])

    def open_row(self, row, column):
        if column != 0:
            self.add_queue(self.visible_scores[row], True)

    def select_score(self, score):
        self.autoplay.stop()
        self.player.load(score)
        self.current = score
        self.update_now_playing()

    def update_now_playing(self):
        score = self.current
        if not score:
            return
        self.now_title.setText(score.name)
        self.now_title.setToolTip(score.name)
        self.now_meta.setText(" · ".join(x for x in [score.author, score.transcriber] if x))
        self.now_cover.setPixmap(cover_icon(score).pixmap(48, 48))
        favorite = self.store.is_favorite(score.path)
        self.star.setEnabled(True)
        self.star.setText("★" if favorite else "☆")
        self.star.setToolTip(style.tr("unfavorite" if favorite else "favorite"))
        self.star.setAccessibleName(self.star.toolTip())
        self.time.setText(f"{time_text(self.player.position_ms)} / {time_text(score.duration_ms)}")

    def add_queue(self, score, play=False):
        if not any(s.path == score.path for s in self.queue):
            self.queue.append(score)
        self.update_queue()
        self.defer_save()
        if play:
            self.select_score(score)
            self.start_playback()

    def update_queue(self):
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        for score in self.queue:
            item = QListWidgetItem(f"{score.name}\n{time_text(score.duration_ms)} · {score.author or '—'}")
            item.setData(Qt.ItemDataRole.UserRole, str(score.path))
            self.queue_list.addItem(item)
        self.queue_list.blockSignals(False)
        self.queue_heading.setText(f"{style.tr('tip.playlist')} · {len(self.queue)}")
        self.queue_empty.setVisible(not self.queue)
        self.queue_list.setVisible(bool(self.queue))
        self.clear_queue_button.setEnabled(bool(self.queue))

    def reorder_queue(self, *_):
        by_path = {str(s.path): s for s in self.queue}
        self.queue = [
            by_path[self.queue_list.item(i).data(Qt.ItemDataRole.UserRole)]
            for i in range(self.queue_list.count())
        ]
        self.defer_save()

    def queue_play(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        score = next((s for s in self.queue if str(s.path) == path), None)
        if score:
            self.select_score(score)
            self.start_playback()

    def queue_menu(self, point):
        item = self.queue_list.itemAt(point)
        if item is None:
            return
        menu = QMenu(self)
        action = menu.addAction(style.tr("delete"))
        if menu.exec(self.queue_list.mapToGlobal(point)) == action:
            self.queue.pop(self.queue_list.row(item))
            self.update_queue()
            self.defer_save()

    def clear_queue(self):
        self.queue.clear()
        self.autoplay.stop()
        self.update_queue()
        self.defer_save()

    def adjacent_score(self, delta, wrap=True):
        scores = self.queue or self.visible_scores or self.scores
        if not scores:
            return None
        index = next((i for i, s in enumerate(scores) if self.current and s.path == self.current.path), -1)
        if index < 0:
            return scores[-1 if delta < 0 else 0]
        index += delta
        if not wrap and not 0 <= index < len(scores):
            return None
        return scores[index % len(scores)]

    def next_song(self, delta=1, play=True, automatic=False):
        if automatic and self.playback_order == "single" and self.current:
            score = self.current
        elif self.playback_order == "shuffle" and self.queue:
            pool = [s for s in self.queue if not self.current or s.path != self.current.path] or self.queue
            score = random.choice(pool)
        else:
            score = self.adjacent_score(delta, wrap=not (automatic and self.playback_order == "sequence"))
        if score:
            self.select_score(score)
            if play:
                self.start_playback()

    def ensure_audio(self):
        if self.no_audio:
            return True
        try:
            ident = (self.instrument.currentData(), self.pitch_value)
            if self.audio_prepared != ident:
                self.mixer.prepare(*ident)
                self.audio_prepared = ident
            self.mixer.volume = self.volume.value() / 100
            self.mixer.cave = self.cave.isChecked()
            if self.audio_output is None:
                self.audio_output = QtAudioOutput(self.mixer)
            return True
        except Exception as exc:
            self.show_error(str(exc))
            return False

    def start_playback(self):
        self.autoplay.stop()
        if self.current is None:
            if self.visible_scores:
                self.add_queue(self.visible_scores[0])
                self.select_score(self.visible_scores[0])
            else:
                return
        try:
            if self.mode.checkedId() == 1:
                output = WindowsOutput(self.store.data["keys"], self.store.data["hold_ms"])
                countdown = self.store.data["countdown"]
            else:
                if not self.ensure_audio():
                    return
                output, countdown = self.mixer, 0
            self.player.play(output, countdown)
            self.play_button.setText("Ⅱ")
        except (ValueError, RuntimeError) as exc:
            self.show_error(str(exc))

    def toggle_play(self):
        if self.player.state == State.PLAYING:
            self.player.pause()
        elif self.player.state == State.PAUSED:
            self.player.pause(False)
        else:
            self.start_playback()

    def stop(self, *_):
        self.autoplay.stop()
        self.player.stop()
        self.mixer.silence()
        self.play_button.setText("▶")
        self.defer_save()

    def notes_fired(self, keys):
        self.piano.flash(keys)

    def finished_song(self):
        if self.closed or self.player.state != State.FINISHED:
            return
        self.play_button.setText("▶")
        if self.queue and (self.playback_order != "sequence" or self.adjacent_score(1, wrap=False)):
            self.autoplay.start(2000)
        self.defer_save()

    def seek_song(self, value):
        self.autoplay.stop()
        if self.current:
            self.player.seek(value / 10000 * self.current.duration_ms)
            self.defer_save()

    def tick(self):
        if self.current:
            position = self.player.position_ms
            duration = self.current.duration_ms
            if not self.seek.isSliderDown():
                self.seek.setValue(int(position / max(1, duration) * 10000))
            self.time.setText(f"{time_text(position)} / {time_text(duration)}")
        self.play_button.setText("Ⅱ" if self.player.state == State.PLAYING else "▶")
        if self.player.countdown > 0:
            self.status.setText(f"{style.tr('countdown')} {self.player.countdown:.1f} s")
        elif self.player.state in (State.PLAYING, State.PAUSED):
            self.status.setText(
                f"{style.tr('play' if self.player.state == State.PLAYING else 'pause')} · "
                f"{self.mode.checkedButton().text()} · {self.player.speed:.2f}×"
            )
        if self.player.random_speed and abs(self.speed.value() - self.player.speed) >= 0.05:
            self.speed.blockSignals(True)
            self.speed.setValue(self.player.speed)
            self.speed.blockSignals(False)

    def speed_changed(self, value):
        self.player.set_speed(value)
        self.defer_save()

    def set_pitch_selection(self, value):
        self.pitch_value = value
        self.pitch.blockSignals(True)
        self.pitch.setPlaceholderText(pitch_text(value))
        self.pitch.setCurrentIndex(self.pitch.findData(value))
        self.pitch.blockSignals(False)

    def instrument_changed(self, name):
        self.stop()
        self.set_pitch_selection(self.store.data["pitches"].get(name, DEFAULT_PITCH.get(name, 0)))
        self.audio_prepared = None
        self.defer_save()

    def pitch_changed(self, value):
        if value is None:
            return
        self.stop()
        self.pitch_value = value
        self.store.data["pitches"][self.instrument.currentData()] = value
        self.audio_prepared = None
        self.defer_save()

    def cave_changed(self, enabled):
        self.mixer.cave = enabled
        self.defer_save()

    def volume_changed(self, value):
        self.mixer.volume = value / 100
        self.defer_save()

    def manual_press(self, key):
        if key not in self.manual_active and self.ensure_audio():
            self.manual_active.add(key)
            self.mixer.note_on(key)
            self.piano.keys[key].down = True
            self.piano.flash((key,))

    def manual_release(self, key):
        self.manual_active.discard(key)
        self.mixer.note_off(key)
        self.piano.keys[key].down = False
        self.piano.keys[key].update()

    def release_manual(self):
        for key in list(self.manual_active):
            self.manual_release(key)
        self.pressed.clear()
        if self.practice_dialog:
            self.practice_dialog.piano.release_all()

    def start_global_keyboard(self, force=False):
        if sys.platform != "win32" or self.no_global:
            return
        if (self.store.data["global_hotkeys"] or force) and not self.global_keyboard.listener:
            try:
                self.global_keyboard.start()
            except Exception as exc:
                self.status.setText(str(exc))

    def global_key(self, name, down):
        # Local Qt events handle the foreground window. This avoids double triggers.
        if QApplication.activeWindow() is not None:
            return
        if self.practice_dialog and self.practice_dialog.game.isChecked():
            self.dispatch_key(name, down)
        elif self.store.data["global_hotkeys"] and name in (
            "F1",
            "F2",
            "F3",
            "F4",
            "F5",
            "F6",
            "LEFT",
            "RIGHT",
        ):
            if down:
                self.hotkey(name)

    def dispatch_key(self, name, down):
        target = self.practice_dialog
        keys = self.store.data["keys"]
        if name in keys:
            index = keys.index(name)
            if down:
                if target:
                    target.press(index)
                else:
                    self.manual_press(index)
            elif target:
                target.release(index)
            else:
                self.manual_release(index)
        elif down:
            target.hotkey(name) if target else self.hotkey(name)

    def hotkey(self, name):
        if name == "F1":
            if self.player.state in (State.PLAYING, State.PAUSED):
                self.stop()
            else:
                self.player.seek(0)
                self.start_playback()
        elif name == "F2":
            self.toggle_play()
        elif name in ("F3", "F4"):
            self.speed.setValue(self.speed.value() + (-0.1 if name == "F3" else 0.1))
        elif name in ("F5", "F6"):
            self.next_song(-1 if name == "F5" else 1)
        elif name in ("LEFT", "RIGHT"):
            self.player.seek(self.player.position_ms + (-2000 if name == "LEFT" else 2000))

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.ApplicationDeactivate:
            self.release_manual()
            self.volume_button.hide_popup()
        if (
            event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape
            and QApplication.activeWindow() is self and self.volume_button.popup.isVisible()
        ):
            self.volume_button.hide_popup()
            return True
        if (
            event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape
            and QApplication.activeWindow() is self and self.queue_button.isChecked()
        ):
            self.queue_button.setChecked(False)
            return True
        if event.type() not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return super().eventFilter(watched, event)
        if QApplication.activePopupWidget() is not None:
            return False
        active = QApplication.activeWindow()
        if active is not self and active is not self.practice_dialog:
            return False
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox)):
            return False
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            return False
        name = {
            Qt.Key.Key_Left: "LEFT",
            Qt.Key.Key_Right: "RIGHT",
            Qt.Key.Key_Escape: "ESC",
            Qt.Key.Key_Space: "SPACE",
            **{getattr(Qt.Key, f"Key_F{i}"): f"F{i}" for i in range(1, 7)},
        }.get(event.key(), event.text().upper())
        if name not in self.store.data["keys"] and name not in (
            "F1",
            "F2",
            "F3",
            "F4",
            "F5",
            "F6",
            "LEFT",
            "RIGHT",
            "ESC",
        ):
            return False
        if event.isAutoRepeat():
            return True
        down = event.type() == QEvent.Type.KeyPress
        if down:
            self.pressed.add(name)
        else:
            self.pressed.discard(name)
        self.dispatch_key(name, down)
        return True

    def song_menu(self, point):
        row = self.table.rowAt(point.y())
        if row < 0:
            return
        score = self.visible_scores[row]
        menu = QMenu(self)
        menu.addAction(style.tr("addqueue"), lambda: self.add_queue(score))
        favorites = menu.addMenu(style.tr("favorites"))
        for i, collection in enumerate(self.store.data["collections"]):
            action = favorites.addAction(collection["name"])
            action.setCheckable(True)
            action.setChecked(str(score.path) in collection["files"])
            action.triggered.connect(lambda checked=False, index=i: self.favorite(score, index))
        menu.addAction(style.tr("edit"), lambda: self.edit_score(score))
        menu.addAction(style.tr("info"), lambda: self.song_info(score))
        menu.addAction(
            style.tr("location"), lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(score.path.parent)))
        )
        menu.addAction(style.tr("upload"), lambda: self.upload_score(score))
        menu.addSeparator()
        menu.addAction(style.tr("remove"), lambda: self.remove_score(score))
        menu.exec(self.table.viewport().mapToGlobal(point))

    def remove_score(self, score):
        try:
            if self.current and self.current.path == score.path:
                self.stop()
                self.current = None
                self.now_title.setText(style.tr("nosong"))
                self.now_meta.clear()
                self.seek.setValue(0)
            self.store.remove(score.path)
            self.queue = [s for s in self.queue if s.path != score.path]
            self.update_queue()
            self.refresh()
            self.populate_collections()
            self.status.setText("曲谱已移入数据目录的 trash 文件夹，可恢复")
        except OSError as exc:
            self.show_error(str(exc))

    def song_info(self, score):
        QMessageBox.information(
            self,
            style.tr("info"),
            f"{score.name}\n{style.tr('artist')}: {score.author}\n"
            f"{style.tr('transcriber')}: {score.transcriber}\nBPM: {score.bpm:g}\n"
            f"{len(score.notes)} notes · {time_text(score.duration_ms)}\n{score.path}",
        )

    def import_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, style.tr("import"), "", "Scores (*.json *.txt *.mid *.midi)"
        )
        imported, errors = [], []
        for filename in paths:
            path = Path(filename)
            try:
                if path.suffix.lower() in (".mid", ".midi"):
                    dialog = MidiDialog(self, path)
                    if dialog.exec() == QDialog.DialogCode.Accepted:
                        imported.append(self.store.add_score(dialog.score))
                else:
                    imported.append(self.store.import_score(path))
            except (OSError, ValueError, EOFError) as exc:
                errors.append(f"{path.name}: {exc}")
        self.show_page(0)
        self.refresh()
        self.status.setText(f"已导入 {len(imported)} 首曲谱")
        if errors:
            self.show_error("\n".join(errors))

    def edit_score(self, score):
        self.stop()
        self.release_manual()
        dialog = EditorDialog(self, score)
        dialog.exec()
        if dialog.saved:
            try:
                saved = load_score(dialog.score.path)
                if saved.path.parent != self.store.songs_dir.resolve():
                    saved = self.store.import_score(saved.path)
                self.queue = [saved if s.path == saved.path else s for s in self.queue]
                if self.current and self.current.path == saved.path:
                    self.select_score(saved)
                self.update_queue()
                self.show_page(0)
                self.refresh()
            except (OSError, ValueError) as exc:
                self.show_error(str(exc))

    def practice(self):
        if self.current is None:
            row = self.table.currentRow()
            if row >= 0:
                self.select_score(self.visible_scores[row])
            elif self.visible_scores:
                self.select_score(self.visible_scores[0])
        if not self.current or not self.current.notes:
            return
        self.stop()
        self.release_manual()
        dialog = PracticeDialog(self, self.current)
        self.practice_dialog = dialog
        dialog.showMaximized()
        dialog.exec()
        self.practice_dialog = None
        self.release_manual()
        if self.current and self.player.timeline:
            self.player.load(self.current)
        self.update_now_playing()

    def settings(self):
        self.stop()
        self.release_manual()
        self.save_state()
        dialog = SettingsDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.global_keyboard.stop()
        self.store.save()
        self.player.random_speed = self.store.data["random_speed"]
        style.current_language = self.store.data["language"]
        style.apply_theme(QApplication.instance(), self.store.data["theme"], self.store.data["font_size"])
        if self.cloud_client.base_url != self.store.data["cloud_url"]:
            # Existing read requests hold their original client until their job finishes.
            self.cloud_client = CloudClient(self.store.data["cloud_url"])
        self.audio_prepared = None
        self.cloud_generation += 1
        self.cloud_items.clear()
        self.cloud_page = self.cloud_pages = 0
        self.cloud_loading = False
        self.build()
        if self.cloud_client.user:
            self.account.setText(self.cloud_client.user.get("username", style.tr("logout")))
        self.update_now_playing()
        self.update_queue()
        self.filter_library()
        self.start_global_keyboard()

    def load_cloud(self, reset=False):
        if self.closed:
            return
        if reset:
            self.cloud_generation += 1
            self.cloud_page = 0
            self.cloud_items.clear()
            self.cloud_table.setRowCount(0)
        elif self.cloud_loading or (self.cloud_pages and self.cloud_page >= self.cloud_pages):
            return
        generation = self.cloud_generation
        page = self.cloud_page + 1
        self.cloud_loading = True
        self.more_button.setEnabled(False)
        query, sort, difficulty = (
            self.search.text(),
            self.cloud_sort.currentData(),
            self.difficulty.currentData(),
        )
        client = self.cloud_client
        self.library_status.setText("正在加载…")

        def loaded(data):
            if generation != self.cloud_generation or self.closed:
                return
            self.cloud_page = page
            self.cloud_pages = int(data.get("pages", 1))
            self.cloud_items.extend(data.get("items", []))
            self.cloud_table.setRowCount(len(self.cloud_items))
            for row, item in enumerate(self.cloud_items):
                for col, text in enumerate(
                    [
                        item.get("title", ""),
                        item.get("artist", ""),
                        "★" * max(0, min(5, int(item.get("difficulty", 0)))),
                        str(item.get("downloads", 0)),
                    ]
                ):
                    self.cloud_table.setItem(row, col, QTableWidgetItem(str(text)))
                self.cloud_table.setRowHeight(row, 48)
            self.library_status.setText(
                f"{len(self.cloud_items)} / {data.get('total', 0)} · {self.cloud_page}/{self.cloud_pages}"
            )

        def finished():
            if generation == self.cloud_generation:
                self.cloud_loading = False
                self.more_button.setEnabled(self.cloud_page < self.cloud_pages)

        def failed(error):
            if generation == self.cloud_generation:
                self.library_status.setText(error)
                self.cloud_pages = max(1, self.cloud_pages)

        self.run_job(lambda: client.list(query, sort, difficulty, page), loaded, failed, finished)

    def cloud_scroll(self, value):
        bar = self.cloud_table.verticalScrollBar()
        if bar.maximum() > 0 and value >= bar.maximum() - 2:
            self.load_cloud(False)

    def download_cloud(self):
        row = self.cloud_table.currentRow()
        if not 0 <= row < len(self.cloud_items):
            return
        item, client = copy.deepcopy(self.cloud_items[row]), self.cloud_client
        self.download_button.setEnabled(False)

        def downloaded(score):
            self.store.add_score(score)
            self.status.setText("已下载: " + score.name)
            self.library_loaded(scan_library(self.store.songs_dir))

        self.run_job(
            lambda: client.download(item),
            downloaded,
            on_finished=lambda: self.download_button.setEnabled(True),
        )

    def login(self):
        if self.cloud_client.user:
            client = self.cloud_client
            if self.store.data.get("remember_login"):
                self.run_job(lambda: clear_auth(client.base_url))
            self.store.data["remember_login"] = False
            self.cloud_client.user.clear()
            self.account.setText(style.tr("login"))
            self.account.setIcon(QIcon())
            self.defer_save()
            return
        dialog = LoginDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        username, password = dialog.username.text(), dialog.password.text()
        client = self.cloud_client
        remember = dialog.remember.isChecked()
        self.account.setEnabled(False)

        def logged_in(user):
            self.restore_auth(user)
            self.store.data["remember_login"] = remember
            self.defer_save()
            if remember:
                self.run_job(
                    lambda: save_auth(client.base_url, user),
                    on_error=lambda message: self.status.setText("无法记住登录: " + message),
                )

        self.run_job(
            lambda: client.login(username, password),
            logged_in,
            on_finished=lambda: self.account.setEnabled(True),
        )
        dialog.password.clear()

    def restore_auth(self, user):
        if not user:
            return
        self.cloud_client.user = user
        self.account.setText(user.get("username", style.tr("logout")))
        self.account.setToolTip(style.tr("logout"))
        client = self.cloud_client
        self.run_job(client.avatar, self.avatar_loaded, on_error=lambda message: None)

    def avatar_loaded(self, data):
        from PySide6.QtGui import QPixmap

        pixmap = QPixmap()
        if data and pixmap.loadFromData(data):
            self.account.setIcon(QIcon(pixmap))
            self.account.setIconSize(QSize(28, 28))

    def upload_score(self, score):
        if not self.cloud_client.user:
            self.login()
            self.status.setText("登录完成后请重新选择上传")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(style.tr("upload"))
        root = QVBoxLayout(dialog)
        root.addWidget(label(score.name, "heading"))
        difficulty = QSpinBox()
        difficulty.setRange(1, 5)
        tags = QLineEdit()
        description = QTextEdit()
        form = QFormLayout()
        form.addRow("难度", difficulty)
        form.addRow("标签", tags)
        form.addRow("说明", description)
        root.addLayout(form)
        root.addWidget(button("upload", dialog.accept, accent=True))
        root.addWidget(button("cancel", dialog.reject))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            client = self.cloud_client
            diff, tag_text, desc = difficulty.value(), tags.text(), description.toPlainText()
            self.run_job(
                lambda: client.upload(score, diff, tag_text, desc),
                lambda result: self.status.setText("上传成功"),
            )

    def about(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("AutoSkyPlay")
        root = QVBoxLayout(dialog)
        root.addWidget(label(f"AutoSkyPlay {__version__}", "heading"))
        root.addWidget(
            label(
                "Python / PySide6 · GPL-3.0\n基于 SMAP 的功能与曲谱协议重新实现。\n音色采样、标志与翻译数据来源见 THIRD_PARTY_NOTICES.md。"
            )
        )
        root.addWidget(
            button(
                "location", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.directory)))
            )
        )
        root.addWidget(button("set.update", self.check_updates))
        root.addWidget(button("set.uploadLog", self.upload_log))
        root.addWidget(button("back", dialog.accept))
        dialog.exec()

    def check_updates(self):
        repo = self.store.data["update_repo"]

        def checked(release):
            if release:
                self.show_release(release)
            else:
                self.status.setText("当前已是最新版本")

        self.run_job(lambda: check_release(repo), checked)

    def show_release(self, release):
        dialog = QDialog(self)
        cancelled = threading.Event()
        dialog.finished.connect(lambda result: cancelled.set())
        dialog.setWindowTitle("AutoSkyPlay · Update")
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("AutoSkyPlay " + release["tag_name"], "heading"))
        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(release.get("body", ""))
        layout.addWidget(notes)
        progress = QProgressBar()
        progress.hide()
        layout.addWidget(progress)
        asset = installer_asset(release)
        if asset:
            download = QPushButton("下载安装更新" if sys.platform == "win32" else "下载更新")
            layout.addWidget(download)

            def begin():
                progress.show()
                download.setEnabled(False)
                job = Job(
                    lambda: download_asset(
                        asset,
                        self.store.directory / "updates",
                        job.signals.progress.emit,
                        lambda: self.closed or cancelled.is_set(),
                    )
                )
                self.jobs.add(job)
                job.signals.progress.connect(progress.setValue)

                def downloaded(path):
                    if self.closed or cancelled.is_set():
                        return
                    self.stop()
                    if sys.platform == "win32":
                        subprocess.Popen([str(path), "/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"])
                        dialog.accept()
                        self.close()
                    else:
                        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
                        dialog.accept()

                job.signals.result.connect(downloaded)
                job.signals.error.connect(
                    lambda message: self.show_error(message) if not cancelled.is_set() else None
                )
                job.signals.finished.connect(lambda: self.jobs.discard(job))
                self.pool.start(job)

            download.clicked.connect(begin)
        layout.addWidget(button("location", lambda: QDesktopServices.openUrl(QUrl(release["html_url"]))))
        layout.addWidget(button("cancel", dialog.reject))
        dialog.resize(560, 430)
        dialog.exec()

    def upload_log(self):
        path = self.store.directory / "autoskyplay.log"
        if not path.exists():
            return
        client = self.cloud_client
        self.run_job(lambda: client.upload_log(path), lambda result: self.status.setText("日志上传成功"))

    def show_error(self, message):
        if self.closed:
            return
        log.error("%s", message)
        self.status.setText(str(message))
        QMessageBox.warning(self, "AutoSkyPlay", str(message))

    def defer_save(self, *_):
        if hasattr(self, "save_timer"):
            self.save_timer.start(500)

    def save_state(self):
        try:
            self.store.data.update(
                current=str(self.current.path) if self.current else "",
                position_ms=self.player.position_ms,
                queue=[str(s.path) for s in self.queue],
                instrument=self.instrument.currentData(),
                speed=self.speed.value(),
                random_speed=self.player.random_speed,
                cave=self.cave.isChecked(),
                volume=self.volume.value() / 100,
                loop=self.playback_order,
            )
            self.store.save()
        except OSError as exc:
            log.error("配置保存失败: %s", exc)
            self.status.setText(str(exc))

    def closeEvent(self, event):
        self.closed = True
        self.timer.stop()
        self.autoplay.stop()
        self.save_timer.stop()
        self.player.stop()
        self.release_manual()
        self.global_keyboard.stop()
        self.save_state()
        self.mixer.silence()
        if self.audio_output:
            self.audio_output.close()
        QApplication.instance().removeEventFilter(self)
        event.accept()
