"""NetEase to local melody transcription and audition window."""

from __future__ import annotations

import base64
import copy
import hashlib
import queue
import shutil
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..audio import INSTRUMENTS
from ..netease import NeteaseAudio, NeteaseSong
from ..playback import Player, State
from ..transcription.arrangement import SOURCE_NAMES, arrange, build_score
from ..transcription.cache import DownloadCache
from ..transcription.drafts import DraftStore
from ..transcription.engine import (
    HYBRID_COMPONENT_VERSION,
    MUSCRIPTOR_VERSION,
    install_engine,
    select_engine,
    select_hybrid_engine,
)
from ..transcription.engine import find_engine as find_runtime
from ..transcription.events import EventResult
from ..transcription.local import MidiResult, read_midi
from ..transcription.melody import export_midi
from ..transcription.models import ModelStore
from ..transcription.performance import generation_mode
from ..transcription.source import AudioSource, load_waveform
from ..transcription.task import TranscriptionTask
from . import style
from .device_status import DeviceStatus
from .editor import EditorDialog, encode_cover
from .netease import NeteaseSongDialog, ServiceDialog
from .transcription_widgets import PreviewRoll, RollKeys, Waveform, clock_text
from .widgets import label

STAGE_NAMES = {
    "load": "加载本地模型",
    "download": "下载音频", "decode": "解码片段", "separate": "分离人声与乐器",
    "pitch": "识别音高", "convert": "整理旋律", "ready": "识别完成",
}


def find_engine(directory):
    return find_runtime(directory, 'muscriptor')


class TranscriptionDialog(ServiceDialog):
    component_progress = Signal(int)
    preview_done = Signal()
    preview_error = Signal(str)

    def __init__(self, host):
        super().__init__(host, host)
        self.setWindowTitle(style.tr("transcribe"))
        self.resize(1280, 820)
        self.setMinimumSize(980, 740)
        self.song = self.audio = self.result = self.score = self.task = None
        self.cover = None
        self.busy = self.saved = self.installing = self.edited = False
        self.restoring = False
        self.draft_id = None
        self.source_overrides = []
        self.draft_title = None
        self.draft_write_failed = False
        self.media_dir = self.source_dir = None
        self.source_job = None
        self.wave_data = None
        self.audition_source = False
        self.media_offset = 0
        self.pending_seek = None
        self.drafts = DraftStore(host.store.directory)
        self.install_cancelled = threading.Event()
        self.player = Player(on_done=self.preview_done.emit, on_error=self.preview_error.emit)
        self.preview_done.connect(self.playback_finished)
        self.preview_error.connect(self.failed)
        self.media = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(host.store.data["volume"])
        self.media.setAudioOutput(self.audio_output)
        self.media.errorOccurred.connect(lambda *_: self.failed("音频试听失败，请恢复音频或检查输出设备"))
        self.media.mediaStatusChanged.connect(self.media_ready)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(10)
        header = QHBoxLayout()
        header.addWidget(label(style.tr("transcribe"), "heading"))
        header.addStretch()
        self.account = label("", "muted")
        self.login = QPushButton("网易云登录")
        self.login.clicked.connect(host.netease_login)
        header.addWidget(self.account)
        header.addWidget(self.login)
        self.engine_status = label("", "muted")
        self.install_button = QPushButton("组件管理")
        self.install_button.clicked.connect(self.install_component)
        header.addWidget(self.engine_status)
        header.addWidget(self.install_button)
        root.addLayout(header)
        self.description = label("钢琴默认主旋律＋稀疏和弦，小提琴默认主旋律；不确定段落保留休止并标记。", "muted")
        root.addWidget(self.description)
        host.netease_auth_changed.connect(self.update_account)
        song_row = QHBoxLayout()
        self.fetch = QPushButton("选择歌曲")
        self.fetch.clicked.connect(self.choose_song)
        song_row.addWidget(self.fetch)
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(48, 48)
        song_row.addWidget(self.cover_label)
        self.song_info = label("选择歌曲，试听并标记需要扒谱的片段。")
        self.song_info.setTextFormat(Qt.TextFormat.PlainText)
        self.song_info.setWordWrap(True)
        song_row.addWidget(self.song_info, 1)
        self.restore_audio = QPushButton("恢复音频")
        self.restore_audio.clicked.connect(self.recover_audio)
        self.restore_audio.hide()
        song_row.addWidget(self.restore_audio)
        root.addLayout(song_row)
        self.identifier = QLineEdit(self)
        self.identifier.hide()
        self.whole = QCheckBox(self)
        self.whole.hide()
        self.waveform = Waveform()
        self.waveform.range_changed.connect(self.wave_range_changed)
        self.waveform.seek_requested.connect(self.wave_seek)
        root.addWidget(self.waveform)
        ranges = QHBoxLayout()
        self.start_time = QDoubleSpinBox()
        self.start_time.setRange(0, 86400)
        self.start_time.setDecimals(2)
        self.end_time = QDoubleSpinBox()
        self.end_time.setRange(.1, 86400)
        self.end_time.setDecimals(2)
        self.length = QDoubleSpinBox(self)
        self.length.setRange(.1, 600)
        self.length.setValue(30)
        self.length.hide()
        self.range_info = label("尚未选择范围", "muted")
        ranges.addWidget(label("入点 / 秒"))
        ranges.addWidget(self.start_time)
        ranges.addWidget(label("出点 / 秒"))
        ranges.addWidget(self.end_time)
        ranges.addWidget(self.range_info, 1)
        self.all_range = QPushButton("全部可用范围")
        self.all_range.clicked.connect(self.select_all_range)
        self.short = QPushButton("选取 30 秒")
        self.short.clicked.connect(self.select_short_range)
        ranges.addWidget(self.all_range)
        ranges.addWidget(self.short)
        for title, action in (("−", lambda: self.waveform.zoom(1.5)), ("+", lambda: self.waveform.zoom(.67)),
                              ("全曲", self.waveform.fit)):
            b = QPushButton(title)
            b.clicked.connect(action)
            ranges.addWidget(b)
        root.addLayout(ranges)
        self.start_time.valueChanged.connect(self.numeric_range_changed)
        self.end_time.valueChanged.connect(self.numeric_range_changed)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.history_panel = QWidget()
        history = QVBoxLayout(self.history_panel)
        history.setContentsMargins(0, 0, 8, 0)
        history.addWidget(label("历史草稿", "brand"))
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("搜索草稿")
        self.history_search.textChanged.connect(self.refresh_history)
        history.addWidget(self.history_search)
        self.history_list = QListWidget()
        self.history_list.itemClicked.connect(self.open_draft)
        history.addWidget(self.history_list, 1)
        self.history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history_list.customContextMenuRequested.connect(self.draft_menu)
        self.splitter.addWidget(self.history_panel)
        center = QWidget()
        central = QVBoxLayout(center)
        central.setContentsMargins(0, 0, 0, 0)
        toolbar = QHBoxLayout()
        toggle = QPushButton("草稿栏")
        toggle.clicked.connect(lambda: self.history_panel.setVisible(self.history_panel.isHidden()))
        toolbar.addWidget(toggle)
        self.result_title = label("曲谱预览", "brand")
        toolbar.addWidget(self.result_title, 1)
        self.display_grid = QComboBox()
        for text, value in (("网格 1/8", 2), ("网格 1/16", 4), ("网格 1/32", 8), ("八分三连音", 3), ("十六分三连音", 6)):
            self.display_grid.addItem(text, value)
        self.display_grid.setCurrentIndex(1)
        self.display_grid.currentIndexChanged.connect(self.update_roll_grid)
        toolbar.addWidget(self.display_grid)
        central.addLayout(toolbar)
        layers = QHBoxLayout()
        self.show_melody = QCheckBox('主旋律')
        self.show_accompaniment = QCheckBox('伴奏')
        for checkbox in (self.show_melody, self.show_accompaniment):
            checkbox.setChecked(True)
            checkbox.toggled.connect(self.layers_changed)
            layers.addWidget(checkbox)
        self.section_list = QComboBox()
        self.section_list.setMinimumWidth(160)
        self.section_list.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.section_list.setToolTip('选择段落定位；待修正段落保留休止。')
        self.section_list.currentIndexChanged.connect(self.section_selected)
        layers.addWidget(self.section_list, 1)
        self.source_choice = QComboBox(self)
        self.source_choice.hide()
        for title, value in (('自动选择来源', 'auto'), ('人声优先', 'vocals'), ('器乐优先', 'other'), ('保留休止', 'rest')):
            self.source_choice.addItem(title, value)
        self.apply_source = QPushButton('段落来源 ▾')
        self.apply_source.clicked.connect(self.section_source_menu)
        layers.addWidget(self.apply_source)
        central.addLayout(layers)
        self.refine_button = QPushButton('小节／片段精修…')
        self.refine_button.setToolTip('仅支持 MuScriptor 原混音草稿；生成候选后对照试听，接受为新版本。')
        self.refine_button.clicked.connect(self.refine_region)
        toolbar.addWidget(self.refine_button)
        roll_row = QHBoxLayout()
        self.roll = PreviewRoll()
        self.roll.seek_requested.connect(self.roll_seek)
        roll_row.addWidget(RollKeys(self.roll))
        roll_row.addWidget(self.roll, 1)
        central.addLayout(roll_row, 1)
        zoom = QHBoxLayout()
        self.context_label = label("识别完成后显示光遇 15 键曲谱", "muted")
        self.context_label.setWordWrap(True)
        zoom.addWidget(self.context_label, 1)
        for title, action in (("−", lambda: self.roll.zoom(.8)), ("+", lambda: self.roll.zoom(1.25)),
                              ("适应全段", self.roll.fit)):
            b = QPushButton(title)
            b.clicked.connect(action)
            zoom.addWidget(b)
        central.addLayout(zoom)
        audition = QHBoxLayout()
        self.preview_mode = QComboBox()
        self.preview_mode.addItems(["原曲", "分离人声", "光遇曲谱", "器乐", "低音", "混合背景"])
        self.preview_mode.currentIndexChanged.connect(self.switch_preview)
        self.preview = QPushButton("播放")
        self.preview.clicked.connect(self.toggle_preview)
        self.rewind = QPushButton("回到开头")
        self.rewind.clicked.connect(lambda: self.seek_preview(0))
        self.loop = QCheckBox("循环")
        self.time_label = label("00:00 / 00:00", "muted")
        for w in (self.preview_mode, self.preview, self.rewind, self.loop):
            audition.addWidget(w)
        audition.addStretch()
        audition.addWidget(self.time_label)
        central.addLayout(audition)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.sliderMoved.connect(self.seek_preview)
        central.addWidget(self.seek)
        tone = QHBoxLayout()
        tone.addWidget(label("演奏音色", "muted"))
        self.instrument = QComboBox()
        for name in INSTRUMENTS:
            self.instrument.addItem(style.tr(name), name)
        self.instrument.setCurrentIndex(max(0, self.instrument.findData(host.instrument.currentData())))
        self.instrument.setToolTip('按此音色生成和试听：钢琴考虑自然音尾，小提琴默认只生成主旋律。')
        self.instrument.currentIndexChanged.connect(self.tone_changed)
        tone.addWidget(self.instrument, 1)
        tone.addWidget(label("音量", "muted"))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(host.volume.value())
        self.volume.setMaximumWidth(120)
        self.volume.valueChanged.connect(self.update_volume)
        tone.addWidget(self.volume)
        central.addLayout(tone)
        self.splitter.addWidget(center)
        side = QWidget()
        side.setMinimumWidth(225)
        settings = QVBoxLayout(side)
        settings.setContentsMargins(8, 0, 0, 0)
        settings.setSpacing(6)
        settings.addWidget(label("曲谱生成参数", "brand"))
        self.backend_choice = QComboBox()
        self.backend_choice.addItem('Demucs＋GAME＋MuScriptor · 默认', 'hybrid')
        self.backend_choice.addItem('MuScriptor 原混音', 'muscriptor')
        self.backend_choice.setToolTip('混合后端使用 GAME medium 识别人声、MuScriptor 识别背景；更耗时，效果需按歌曲复核。')
        settings.addWidget(self.backend_choice)
        self.model_choice = QComboBox()
        self.model_choice.addItem('MuScriptor small · 默认', 'small')
        self.model_choice.addItem('MuScriptor medium', 'medium')
        self.model_choice.setToolTip('用于下一次识别；切换模型不会覆盖已有草稿。模型在主页面设置中管理。')
        settings.addWidget(self.model_choice)
        self.device_choice = QComboBox()
        self.device_choice.addItem('自动选择加速设备', 'auto')
        self.device_choice.addItem('CPU（兼容模式）', 'cpu')
        self.device_choice.setToolTip('逐设备验证 CUDA；MuScriptor 可使用 MPS。自动模式的 GPU 故障会用 CPU 重试当前阶段一次。')
        settings.addWidget(self.device_choice)
        self.device_status = DeviceStatus(self)
        self.device_status.refresh_button.clicked.connect(lambda: self.refresh_devices(force=True))
        settings.addWidget(self.device_status)
        self.model_choice.currentIndexChanged.connect(self.update_engine)
        self.backend_choice.currentIndexChanged.connect(self.update_engine)
        self.device_choice.currentIndexChanged.connect(self.update_engine)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(8)
        self.generation_mode = QComboBox()
        self.generation_mode.addItem('按音色推荐', 'auto')
        self.generation_mode.addItem('主旋律＋稀疏单音伴奏', 'simple')
        self.generation_mode.addItem('钢琴主旋律＋稀疏和弦', 'chords')
        self.generation_mode.addItem('主旋律＋和弦', 'rich_chords')
        self.generation_mode.addItem('仅完整主旋律', 'melody')
        self.generation_mode.setToolTip('主旋律＋和弦：依据原曲和声适度补全，有旋律时伴奏对齐旋律起音，总共最多三键；独立器乐段保留原节奏，支持全部音色。按音色推荐仍使用原默认。声部勾选只控制显示与试听。')
        form.addRow('生成模式', self.generation_mode)
        self.rhythm_mode = QComboBox()
        for title, value in (('轻度整理（可靠拍点）', 'gentle'), ('原始时序', 'original'), ('严格对齐', 'strict')):
            self.rhythm_mode.addItem(title, value)
        form.addRow('节奏整理', self.rhythm_mode)
        self.ornaments = QComboBox()
        self.ornaments.addItem('简化装饰音', 'simplify')
        self.ornaments.addItem('保留装饰音', 'keep')
        form.addRow('装饰音', self.ornaments)
        self.auto_bpm = QCheckBox("自动")
        self.auto_bpm.setChecked(True)
        self.bpm = QDoubleSpinBox()
        self.bpm.setRange(30, 300)
        self.bpm.setDecimals(2)
        self.bpm.setValue(120)
        self.bpm.setEnabled(False)
        bpm_row = QHBoxLayout()
        bpm_row.addWidget(self.auto_bpm)
        bpm_row.addWidget(self.bpm, 1)
        form.addRow("BPM", bpm_row)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(.05, .95)
        self.threshold.setSingleStep(.05)
        self.threshold.setValue(.35)
        self.threshold.setToolTip("调低可保留更多音符，也可能增加杂音；数值不是准确率。")
        form.addRow("过滤阈值", self.threshold)
        self.minimum = QSpinBox()
        self.minimum.setRange(20, 1000)
        self.minimum.setValue(80)
        self.minimum.setSuffix(" ms")
        self.minimum.setToolTip("调高可减少碎音，也可能漏掉快速音符。")
        form.addRow("最短音符", self.minimum)
        self.auto_shift = QCheckBox("自动")
        self.auto_shift.setChecked(True)
        self.shift = QSpinBox()
        self.shift.setRange(-48, 48)
        self.shift.setSuffix(" 半音")
        shift_row = QHBoxLayout()
        shift_row.addWidget(self.auto_shift)
        shift_row.addWidget(self.shift, 1)
        form.addRow("移调", shift_row)
        self.grid = QComboBox()
        for name, value in (("自动分格 · 1/16", 0), ("吸附 1/8", 2), ("吸附 1/16", 4), ("吸附 1/32", 8), ("八分三连音", 3), ("十六分三连音", 6)):
            self.grid.addItem(name, value)
        form.addRow("节拍吸附", self.grid)
        self.grid_offset = QDoubleSpinBox()
        self.grid_offset.setRange(-60000, 60000)
        self.grid_offset.setSuffix(" ms")
        self.grid_offset.setSingleStep(10)
        self.grid_offset.setToolTip("拍点偏移；检测的是节拍脉冲，不代表小节第一拍。手动 BPM 使用固定网格。")
        form.addRow("网格起点", self.grid_offset)
        self.param_form = form
        form_host = QWidget()
        self.param_host = form_host
        form_host.setLayout(form)
        for widget in [self.bpm, self.threshold, self.minimum, self.shift, self.grid, self.grid_offset]:
            widget.setMinimumHeight(32)
        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        form_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        form_scroll.setMinimumHeight(90)
        form_host.setMinimumHeight(365)
        form.setRowVisible(self.grid_offset, False)
        form_scroll.setWidget(form_host)
        settings.addWidget(form_scroll, 1)
        info = label("调参使用已有识别结果，无需再次运行模型。", "muted")
        info.setWordWrap(True)
        info.setMinimumHeight(34)
        settings.addWidget(info)
        self.statistics = label("", "muted")
        self.statistics.setWordWrap(True)
        settings.addWidget(self.statistics)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        settings.addWidget(self.progress)
        self.status = label("选择歌曲以开始", "muted")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        settings.addWidget(self.status)
        self.begin = QPushButton("开始扒谱")
        self.begin.setObjectName("accent")
        self.begin.clicked.connect(self.start_task)
        self.cancel_button = QPushButton("取消任务")
        self.cancel_button.clicked.connect(self.cancel_task)
        settings.addWidget(self.begin)
        settings.addWidget(self.cancel_button)
        self.splitter.addWidget(side)
        self.splitter.setSizes([205, 700, 275])
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setCollapsible(1, False)
        self.splitter.setCollapsible(2, False)
        root.addWidget(self.splitter, 1)
        output = QHBoxLayout()
        self.draft_status = label("草稿将在识别后自动保存", "muted")
        output.addWidget(self.draft_status, 1)
        self.edit = QPushButton("编辑曲谱")
        self.edit.clicked.connect(self.edit_score)
        self.save = QPushButton("保存到曲库")
        self.save.setObjectName("accent")
        self.save.clicked.connect(self.save_score)
        self.midi = QPushButton("导出原始 MIDI")
        self.midi.setToolTip("导出识别到的人声／器乐／低音，保留来源音轨；不含自动生成伴奏、光遇映射及人工编辑。")
        self.midi.clicked.connect(self.save_midi)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        for widget in (self.edit, self.save, self.midi, close):
            output.addWidget(widget)
        root.addLayout(output)
        self.remap_timer = QTimer(self)
        self.remap_timer.setSingleShot(True)
        self.remap_timer.setInterval(250)
        self.remap_timer.timeout.connect(self.remap)
        for widget in (self.threshold, self.minimum, self.shift, self.bpm, self.grid_offset):
            widget.valueChanged.connect(self.schedule_remap)
        self.auto_shift.toggled.connect(self.schedule_remap)
        self.auto_bpm.toggled.connect(self.schedule_remap)
        self.grid.currentIndexChanged.connect(self.schedule_remap)
        for combo in (self.generation_mode, self.rhythm_mode, self.ornaments):
            combo.currentIndexChanged.connect(self.schedule_remap)
        self.component_progress.connect(self.progress.setValue)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(50)
        self.update_engine()
        self.set_busy(False)
        self.refresh_history()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "roll"):
            compact = self.height() < 820
            self.description.setVisible(not compact)
            self.roll.setMinimumHeight(120 if compact else 170)
        if hasattr(self, "history_panel"):
            if self.width() < 1120 and not self.history_panel.isHidden():
                self.history_panel.hide()
                self._auto_collapsed = True
            elif self.width() >= 1120 and getattr(self, "_auto_collapsed", False):
                self.history_panel.show()
                self._auto_collapsed = False

    def update_account(self):
        user = self.client.user
        self.account.setText(str(user.get('nickname', '未登录网易云')))
        self.account.setTextFormat(Qt.TextFormat.PlainText)
        self.login.setVisible(not bool(user))
        self.login.setEnabled(not self.host.netease_auth_restoring and not self.busy)

    def update_engine(self):
        self.refresh_devices()
        try:
            if self.backend_choice.currentData() == 'hybrid':
                engine = select_hybrid_engine(self.host.store.directory, self.model_choice.currentData(), self.device_choice.currentData())
                suffix = '就绪'
                if sys.platform == 'win32' and not engine.config.get('accelerated_stages', True):
                    suffix = f'旧组件：升级至 {HYBRID_COMPONENT_VERSION} 可启用全阶段 CUDA'
                self.engine_status.setText(f'GAME medium / MuScriptor {self.model_choice.currentData()} · {suffix}')
                return
            engine = find_engine(self.host.store.directory)
            size = self.model_choice.currentData()
            installed = ModelStore(self.host.store.directory).installed(size)
            self.engine_status.setText(f'MuScriptor {size} · ' +
                                      ('就绪' if engine and installed else '未安装运行组件' if not engine else '请在设置中下载模型'))
        except ValueError as exc:
            self.engine_status.setText(str(exc))

    def refresh_devices(self, force=False):
        try:
            directory = self.host.store.directory
            engine = (select_hybrid_engine(directory, require_models=False)
                      if self.backend_choice.currentData() == 'hybrid' else find_engine(directory))
            if engine:
                self.device_status.refresh(engine, self.device_choice.currentData(), force)
            else:
                self.device_status.stop()
                self.device_status.summary.setText('请先安装运行组件以检测设备')
        except (ValueError, OSError) as exc:
            self.device_status.stop()
            self.device_status.summary.setText(str(exc))

    def set_busy(self, busy):
        if busy:
            self.stop_preview()
        self.busy = busy
        self.device_status.refresh_button.setEnabled(not busy)
        if busy:
            self.device_status.stop()
        for widget in (self.fetch, self.install_button, self.backend_choice, self.model_choice, self.device_choice, self.history_list, self.start_time, self.end_time, self.all_range, self.short,
                       self.waveform, self.restore_audio, self.instrument, self.generation_mode, self.rhythm_mode,
                       self.ornaments, self.source_choice, self.apply_source, self.section_list):
            widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.cancel_button.setVisible(busy)
        self.progress.setVisible(busy)
        self.begin.setEnabled(not busy and self.audio is not None)
        for widget in (self.threshold, self.minimum, self.auto_shift, self.auto_bpm, self.grid, self.grid_offset):
            widget.setEnabled(not busy)
        self.shift.setEnabled(not busy and not self.auto_shift.isChecked())
        self.bpm.setEnabled(not busy and not self.auto_bpm.isChecked())
        self.grid.setEnabled(not busy and self.rhythm_mode.currentData() != 'original')
        self.grid_offset.setEnabled(not busy and bool(self.grid.currentData()))
        self.param_form.setRowVisible(self.grid_offset, bool(self.grid.currentData()))
        self.param_host.setMinimumHeight(405 if self.grid.currentData() else 365)
        self.update_output()
        self.update_account()

    def update_output(self):
        from ..transcription.refinement import eligible
        rich = self.generation_mode.currentData() == 'rich_chords'
        self.description.setText(
            '主旋律＋和弦：伴奏对齐主旋律起音，总共最多三键；保留旋律音区和音尾保护，无旋律段沿用原节奏。'
            if rich else '钢琴默认主旋律＋稀疏和弦，小提琴默认主旋律；不确定段落保留休止并标记。')
        self.generation_mode.model().item(self.generation_mode.findData('chords')).setEnabled(
            self.instrument.currentData() == 'Piano')
        self.refine_button.setEnabled(not self.busy and eligible(self.result) and self.score is not None
                                      and self.source_dir is not None and (self.source_dir/'source.audio').exists())
        for widget in (self.edit, self.save):
            widget.setEnabled(not self.busy and self.score is not None)
        self.midi.setEnabled(not self.busy and self.result is not None)
        playable = self.result is not None or (self.source_dir and (self.source_dir / 'source.audio').exists())
        for widget in (self.preview, self.preview_mode, self.seek, self.rewind):
            widget.setEnabled(not self.busy and bool(playable))
        self.preview_mode.model().item(1).setEnabled(bool(self.media_dir and (self.media_dir / 'vocals.wav').exists()))
        self.preview_mode.model().item(2).setEnabled(self.score is not None and bool(self.score.notes))
        for index, stem in ((3, 'other'), (4, 'bass'), (5, 'background')):
            self.preview_mode.model().item(index).setEnabled(bool(self.media_dir and (self.media_dir / (stem + '.wav')).exists()))
        self.apply_source.setEnabled(not self.busy and self.result is not None and not isinstance(self.result, MidiResult))
        midi = isinstance(self.result, MidiResult)
        self.begin.setText('转换 MIDI 选段' if midi else '重新识别' if self.result else '开始扒谱')
        native = isinstance(self.result, EventResult) or self.result is None
        self.threshold.setEnabled(not self.busy and not midi and not native)
        self.param_form.setRowVisible(self.threshold, not native and not midi)
        self.ornaments.setEnabled(not self.busy and not native and not midi)
        self.ornaments.setToolTip(
            '不适用：保留原始音符事件。此后端不进行旧式音高平滑；音频结果仍受最短音符过滤影响。'
            if native or midi else
            '装饰音是倚音、短经过音等旋律细节。简化会平滑短暂音高抖动或八度毛刺；保留会留下更多细节，也可能留下误识别。仍受最短音符过滤影响，不增加和弦。')
        self.param_form.labelForField(self.ornaments).setText('装饰音（不适用）' if native or midi else '装饰音')
        self.minimum.setEnabled(not self.busy and not midi)
        self.save.setText('再次保存到曲库' if self.saved else '保存到曲库')
        missing = self.song is not None and not (self.source_dir and
                   ((self.source_dir / 'source.audio').exists() or (self.source_dir / 'source.mid').exists()))
        self.restore_audio.setVisible(missing or (self.song is not None and self.audio is None))
        if midi:
            self.preview_mode.model().item(0).setEnabled(False)
        else:
            self.preview_mode.model().item(0).setEnabled(True)

    def choose_song(self):
        if self.busy:
            return
        self.flush_remap()
        dialog = NeteaseSongDialog(self, picker=True)
        if not dialog.exec():
            return
        if dialog.local_path:
            self.import_local(dialog.local_path)
        elif dialog.selection:
            song, cover = dialog.selection
            self.identifier.setText(song.id)
            self.fetch_song(cover)

    def import_local(self, path):
        path = Path(path)
        if self.busy:
            return
        self.flush_remap()
        if not self.persist_draft():
            return
        try:
            limit = 10 if path.suffix.lower() in ('.mid', '.midi') else 100
            if not path.is_file() or path.stat().st_size > limit * 1024**2:
                raise ValueError(f'请选择不超过 {limit} MB 的有效文件')
            with path.open('rb') as stream:
                ident = 'local-' + hashlib.file_digest(stream, 'sha256').hexdigest()
        except (OSError, ValueError) as exc:
            self.failed(str(exc))
            return
        self.discard_result()
        self.release_source()
        self.waveform.data = None
        self.waveform.update()
        self.preview_mode.setCurrentIndex(0)
        self.song = NeteaseSong(ident, path.stem, '', '', '', 0)
        self.audio = NeteaseAudio(ident, '', 0)
        self.set_selection(0, 30)
        self.update_song_card()
        if path.suffix.lower() not in ('.mid', '.midi'):
            self.prepare_source(local_path=path)
            return
        from ..midi import MidiImporter
        self.set_busy(True)
        self.status.setText('读取 MIDI 音轨…')
        source = AudioSource(self.host.store.directory, self.client, self.song, self.audio, local_path=path)
        self.source_job = source
        # Copy in this dialog's owned directory; cancelling/closing removes it.
        try:
            shutil.copyfile(path, source.work / 'source.mid')
        except OSError as exc:
            self.audio = None
            self.set_busy(False)
            self.release_source()
            self.failed(str(exc))
            return
        source.completed.set()
        def inspect():
            importer = MidiImporter(source.work / 'source.mid')
            _, duration = read_midi(source.work / 'source.mid', length=86400)
            tracks = [(i, name, count) for i, name, count in importer.tracks() if importer.pitches({i})]
            return tracks, duration
        def ready(value):
            tracks, duration = value
            choices = ['全部非打击乐音轨'] + [f'{i + 1}. {name}（{count} 个音符）' for i, name, count in tracks]
            selected = [i for i, _, _ in tracks]
            if len(tracks) > 1:
                choice, ok = QInputDialog.getItem(self, '选择 MIDI 音轨', '选择主旋律音轨，或保留全部音轨：', choices, 0, False)
                if not ok:
                    self.set_busy(False)
                    self.release_source()
                    self.song = self.audio = None
                    self.update_song_card()
                    self.update_output()
                    return
                if choices.index(choice):
                    selected = [tracks[choices.index(choice) - 1][0]]
            self.source_dir = source.work
            self.song = NeteaseSong(ident, path.stem, '', '', '', duration * 1000)
            self.audio = NeteaseAudio(ident, '', duration * 1000)
            self.wave_data = dict(kind='midi', source_name=path.name, peaks=[], origin=0,
                                  duration=duration, full_duration=duration, trial=False, tracks=selected)
            from ..score import atomic_json
            atomic_json(source.work / 'waveform.json', self.wave_data)
            self.waveform.set_data(self.wave_data)
            self.set_selection(0, min(600, duration))
            self.set_busy(False)
            self.convert_midi_selection()
            self.update_song_card()
        def failed(message):
            self.audio = None
            self.set_busy(False)
            self.failed(message)
        self.job(inspect, ready, failed)

    def convert_midi_selection(self):
        if not self.source_dir or not (self.source_dir / 'source.mid').is_file():
            self.failed('MIDI 源文件缺失，请重新选择本地文件')
            return
        try:
            result, _ = read_midi(self.source_dir / 'source.mid', (self.wave_data or {}).get('tracks'),
                                  self.start_time.value(), self.length.value())
            result.metadata['source_name'] = self.wave_data.get('source_name', 'source.mid')
        except (OSError, ValueError, EOFError) as exc:
            self.failed(str(exc))
            return
        self.stop_preview()
        self.draft_id = None
        self.result = result
        self.edited = self.saved = False
        self.remap()
        self.preview_mode.setCurrentIndex(2)
        self.roll.fit()
        self.status.setText('MIDI 已直接转换并保存草稿；原始文件已保留，无需扒谱组件。')

    def draft_menu(self, position):
        if self.busy:
            return
        item = self.history_list.itemAt(position)
        if item is None:
            return
        self.history_list.setCurrentItem(item)
        menu = QMenu(self)
        menu.addAction('重命名', self.rename_draft)
        menu.addAction('删除草稿', self.delete_draft)
        menu.exec(self.history_list.viewport().mapToGlobal(position))

    def rename_draft(self):
        if self.busy or not self.history_list.currentItem():
            return
        ident = self.history_list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.flush_remap()
        entry = next((d for d in self.drafts.entries() if d['id'] == ident), None)
        if not entry:
            return
        title, ok = QInputDialog.getText(self, '重命名草稿', '草稿名称：',
                                        text=entry.get('title', entry['song']['name']))
        if not ok:
            return
        try:
            self.drafts.rename(ident, title)
            if ident == self.draft_id:
                self.draft_title = title.strip()
                if self.score:
                    self.score.name = self.draft_title
                self.saved = False
                self.persist_draft()
            self.refresh_history()
        except (OSError, ValueError) as exc:
            self.failed(str(exc))

    def fetch_song(self, cover=None):
        if self.busy:
            return
        self.flush_remap()
        value = self.identifier.text().strip()
        if not value:
            self.failed('请填写网易云歌曲链接或 ID')
            return
        if not self.persist_draft():
            return
        self.discard_result()
        self.release_source()
        self.song = self.audio = None
        self.waveform.data = None
        self.waveform.update()
        self.set_busy(True)
        self.status.setText('正在获取歌曲与可用音频…')
        def fetched(value):
            self.song, self.audio = value
            self.cover = cover if isinstance(cover, bytes) else None
            self.update_song_card()
            origin = self.audio.trial_start or 0
            self.set_selection(origin, origin + min(30, self.audio.available_seconds or 30))
            self.set_busy(False)
            self.prepare_source()
        def failed(message):
            self.set_busy(False)
            self.failed(message)
        def lookup():
            song = self.client.detail(value)
            cached = DownloadCache(self.host.store.directory).get(song.id)
            return song, cached or self.client.playback_audio(song.id)
        self.job(lookup, fetched, failed)

    def update_song_card(self):
        if not self.song:
            return
        self.fetch.setText('更换歌曲')
        kind = (self.wave_data or {}).get('kind', 'netease')
        if kind in ('local_audio', 'midi'):
            scope = '本地 MIDI · 直接转换音符' if kind == 'midi' else '本地音频'
            duration = self.song.duration_ms / 1000
        elif self.audio:
            scope = (f'仅试听 {clock_text(self.audio.trial_start)}—{clock_text(self.audio.trial_end)}'
                     if self.audio.is_trial else '完整音频')
            duration = max(self.song.duration_ms, self.audio.duration_ms) / 1000
        else:
            scope = '历史草稿 · 音频范围以保存时为准'
            duration = self.song.duration_ms / 1000
        title = self.song.name + (f' · {self.song.artists}' if self.song.artists else '')
        self.song_info.setText(f'{title}\n{clock_text(duration)} · {scope}')
        self.cover_label.clear()
        if self.cover:
            image = QPixmap()
            if image.loadFromData(self.cover):
                self.cover_label.setPixmap(image.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio,
                                                        Qt.TransformationMode.SmoothTransformation))

    def prepare_source(self, local_path=None):
        self.stop_preview()
        self.release_source()
        job = AudioSource(self.host.store.directory, self.client, self.song, self.audio, local_path=local_path)
        self.source_job = job
        self.set_busy(True)
        self.status.setText('读取本地音频…' if local_path else '检查下载缓存并准备音频…')
        self.progress.setRange(0, 0)
        def prepared(data):
            self.source_dir = job.work
            self.audio = job.audio
            self.wave_data = data
            self.waveform.set_data(data)
            if local_path:
                self.audio = NeteaseAudio(self.song.id, '', data['duration'] * 1000)
                self.song = NeteaseSong(self.song.id, self.song.name, '', '', '', data['duration'] * 1000)
            low, high = data['origin'], data['origin'] + data['duration']
            start = max(low, min(self.start_time.value(), high - .1))
            self.set_selection(start, min(high, start + self.length.value()))
            self.status.setText(('已使用下载缓存。' if job.cached else '') + '拖动两端选择范围，单击波形定位试听。')
            self.update_song_card()
            self.persist_draft()
        def finished(_):
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.set_busy(False)
        def failed(message):
            if local_path:
                self.audio = None
            self.failed(message)
        self.job(job.prepare, prepared, failed, finished)

    def recover_audio(self):
        if self.busy or not self.song:
            return
        if self.result and self.result.metadata.get('source') in ('local_audio', 'midi'):
            self.failed('本地源文件缺失，请通过选择歌曲 → 本地上传重新选择文件。')
            return
        self.set_busy(True)
        def loaded(audio):
            if self.result:
                start = self.result.metadata['source_start']
                end = start + self.result.duration_ms / 1000
                low = audio.trial_start or 0
                if start < low or end > low + audio.available_seconds + .1:
                    self.set_busy(False)
                    self.failed('当前可用音频不包含草稿片段；草稿和曲谱仍可使用。')
                    return
            self.audio = audio
            self.prepare_source()
        self.job(lambda: DownloadCache(self.host.store.directory).get(self.song.id) or
                 self.client.playback_audio(self.song.id), loaded,
                 lambda message: (self.set_busy(False), self.failed(message)))

    def release_source(self):
        self.media.stop()
        self.media.setSource(QUrl())
        if self.source_job:
            self.source_job.close()
        self.source_job = None
        self.source_dir = None
        self.wave_data = None

    def set_selection(self, start, end):
        start, end = round(start, 2), round(end, 2)
        end = max(start + .1, min(start + 600, end))
        for widget, value in ((self.start_time, start), (self.end_time, end), (self.length, end - start)):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        self.waveform.set_range(start, end)
        self.range_info.setText(f'已选 {end - start:.2f} 秒')
        if not self.result:
            self.seek.setRange(0, round((end - start) * 1000))
        elif (abs(start - self.result.metadata.get('source_start', 0)) > .02 or
              abs(end - start - self.result.metadata.get('requested_length', self.result.duration_ms / 1000)) > .02):
            self.status.setText('选段已变化，当前曲谱仍对应上一范围。点击重新识别生成新版本。')

    def numeric_range_changed(self):
        if self.restoring:
            return
        self.stop_preview()
        low = self.wave_data['origin'] if self.wave_data else (self.audio.trial_start or 0) if self.audio else 0
        high = low + (self.wave_data['duration'] if self.wave_data else self.audio.available_seconds if self.audio else 600)
        start = max(low, min(self.start_time.value(), high - .1))
        end = max(start + .1, min(self.end_time.value(), high, start + 600))
        self.set_selection(start, end)

    def wave_range_changed(self, start, end):
        self.stop_preview()
        self.set_selection(start, end)

    def select_all_range(self):
        if self.wave_data:
            start = self.wave_data['origin']
            self.stop_preview()
            self.set_selection(start, start + min(600, self.wave_data['duration']))
            if self.wave_data['duration'] > 600:
                self.status.setText('单次最多 10 分钟，已选择可用音频前 10 分钟。')

    def select_short_range(self):
        if self.wave_data:
            start = self.start_time.value()
            self.stop_preview()
            self.set_selection(start, min(start + 30, self.wave_data['origin'] + self.wave_data['duration']))

    def install_component(self):
        if self.busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, '导入本地扒谱组件包', '', 'Engine package (*.zip)')
        if not path:
            return
        self.install_cancelled.clear()
        self.installing = True
        self.set_busy(True)
        self.progress.setRange(0, 100)
        self.status.setText('正在校验并安装组件…')
        self.job(lambda: install_engine(Path(path), self.host.store.directory,
                                       self.install_cancelled.is_set, self.component_progress.emit),
                 self.component_installed, self.failed, self.install_finished)

    def component_installed(self, engine):
        index = self.backend_choice.findData(engine.backend)
        if index >= 0:
            self.backend_choice.setCurrentIndex(index)
        name = {'hybrid': '混合后端', 'muscriptor': 'MuScriptor'}.get(engine.backend, '扒谱')
        self.status.setText(f'{name} {engine.version} 运行组件安装完成')

    def install_finished(self, _=None):
        self.installing = False
        self.set_busy(False)
        self.update_engine()

    def start_task(self):
        if self.busy or not self.audio:
            return
        self.flush_remap()
        if not self.persist_draft():
            return
        if isinstance(self.result, MidiResult):
            self.convert_midi_selection()
            return
        try:
            if self.backend_choice.currentData() != 'hybrid' and not find_engine(self.host.store.directory):
                raise ValueError(f'请先导入本机平台的 MuScriptor {MUSCRIPTOR_VERSION} 运行组件包')
            selector = select_hybrid_engine if self.backend_choice.currentData() == 'hybrid' else select_engine
            engine = selector(self.host.store.directory, self.model_choice.currentData(), self.device_choice.currentData())
            start, length = self.start_time.value(), self.length.value()
            source = self.source_dir / 'source.audio' if self.source_dir else None
            task = TranscriptionTask(self.host.store.directory, self.client, self.song,
                                     self.audio, engine, start, length, source=source,
                                     source_kind=(self.wave_data or {}).get("kind", "netease"),
                                     source_name=(self.wave_data or {}).get("source_name", ""))
        except ValueError as exc:
            self.failed(str(exc))
            return
        self.stop_preview()
        if self.task:
            self.task.close()
        self.task = task
        self.set_busy(True)
        self.status.setText('开始处理，已有草稿已保留…')
        task.start()

    def discard_result(self):
        self.stop_preview()
        self.remap_timer.stop()
        if self.task:
            self.task.close()
        self.source_overrides = []
        self.section_list.clear()
        self.task = self.result = self.score = None
        self.media_dir = None
        self.cover = None
        self.draft_id = None
        self.draft_title = None
        self.saved = self.edited = False
        self.statistics.setText('')
        self.seek.setValue(0)
        self.roll.set_score(None)
        self.context_label.setText('识别完成后显示光遇 15 键曲谱')
        self.draft_status.setText('草稿将在识别后自动保存')

    def cancel_task(self):
        if self.task and self.task.running:
            self.task.cancel()
            self.status.setText('正在取消任务，已有草稿会保留…')
        elif self.installing:
            self.install_cancelled.set()
            self.status.setText('正在取消组件安装…')
        else:
            if self.source_job:
                self.source_job.close()
            self.generation += 1
            self.progress.setRange(0, 100)
            self.set_busy(False)
            self.status.setText('已取消')

    def poll(self):
        if self.task:
            for _ in range(100):
                try:
                    event = self.task.events.get_nowait()
                except queue.Empty:
                    break
                stage = event['stage']
                if stage == 'ready':
                    self.result, self.cover = event['result'], event.get('cover') or self.cover
                    self.draft_id = None
                    self.source_overrides = []
                    self.edited = self.saved = False
                    self.media_dir = self.task.work
                    self.audition_source = False
                    self.remap()
                    if self.draft_id:
                        self.media_dir = self.drafts.path(self.draft_id)
                    self.update_song_card()
                    self.roll.fit()
                    self.status.setText('识别完成，草稿已自动保存。可试听、调参或编辑。')
                elif stage == 'finished':
                    self.set_busy(False)
                    self.progress.setRange(0, 100)
                    self.progress.setValue(100 if self.result else 0)
                elif stage == 'device':
                    self.device_status.execution(event)
                elif stage in ('error', 'cancelled', 'notice'):
                    self.failed(event.get('message', '处理失败'))
                elif stage in STAGE_NAMES:
                    self.progress.setRange(0, 100)
                    self.progress.setValue(max(0, min(100, int(event.get('progress', 0)))))
                    source = {'vocals': '人声', 'other': '器乐', 'bass': '低音'}.get(event.get('source'), '')
                    self.status.setText(STAGE_NAMES[stage] + (' · ' + source if source else '') + '…')
        playing = self.player.state == State.PLAYING or self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if playing and not self.seek.isSliderDown():
            position = self.player.position_ms if self.preview_mode.currentIndex() == 2 else self.media.position() - self.media_offset
            self.seek.setValue(max(0, round(position)))
            if position >= self.play_duration() - 20:
                self.playback_finished()
        position = self.seek.value()
        self.time_label.setText(f'{clock_text(position / 1000)} / {clock_text(self.play_duration() / 1000)}')
        self.roll.follow = playing
        self.roll.set_position(position if not self.audition_source else
                               position + (self.start_time.value() - self.result_start()) * 1000)
        self.waveform.cursor = (self.start_time.value() if self.audition_source or not self.result else self.result_start()) + position / 1000
        self.waveform.update()

    def parameters(self):
        return dict(backend=self.backend_choice.currentData(), model=self.model_choice.currentData(), device=self.device_choice.currentData(),
                    threshold=self.threshold.value(), minimum=self.minimum.value(), auto_shift=self.auto_shift.isChecked(),
                    shift=self.shift.value(), auto_bpm=self.auto_bpm.isChecked(), bpm=self.bpm.value(),
                    grid=self.grid.currentData(), grid_offset=self.grid_offset.value(),
                    display_grid=self.display_grid.currentData(), instrument=self.instrument.currentData(),
                    mode=self.generation_mode.currentData(), rhythm=self.rhythm_mode.currentData(),
                    ornaments=self.ornaments.currentData(), overrides=self.source_overrides,
                    show_melody=self.show_melody.isChecked(), show_accompaniment=self.show_accompaniment.isChecked())

    def schedule_remap(self, *_):
        if self.restoring:
            return
        self.shift.setEnabled(not self.auto_shift.isChecked() and not self.busy)
        self.bpm.setEnabled(not self.auto_bpm.isChecked() and not self.busy)
        self.grid.setEnabled(not self.busy and self.rhythm_mode.currentData() != 'original')
        self.grid_offset.setEnabled(bool(self.grid.currentData()) and not self.busy)
        self.param_form.setRowVisible(self.grid_offset, bool(self.grid.currentData()))
        self.param_host.setMinimumHeight(405 if self.grid.currentData() else 365)
        self.remap_timer.start()

    def flush_remap(self):
        if self.remap_timer.isActive():
            self.remap_timer.stop()
            self.remap()

    def remap(self, *_):
        if not self.result or self.restoring:
            return
        self.stop_preview()
        if self.edited and self.draft_write_failed and not self.persist_draft():
            return
        self.saved = False
        if self.edited:
            # The edited version is already durable. Remapping always forks it.
            self.draft_id = None
            self.edited = False
        try:
            if self.auto_bpm.isChecked():
                self.bpm.blockSignals(True)
                self.bpm.setValue(self.result.bpm)
                self.bpm.blockSignals(False)
                self.bpm.setToolTip("自动估计值，可关闭自动后校正；不代表第一拍位置。"
                                    if self.result.metadata.get("bpm_estimated") else
                                    "BPM 未可靠估计，暂用 120；可关闭自动后手动设置。")
            if isinstance(self.result, MidiResult):
                self.bpm.setToolTip("读取 MIDI 选段起点的速度；变速信息已换算为音符的真实时间。")
            self.score, stats = build_score(self.result, self.draft_title or self.song.name,
                                            threshold=self.threshold.value(), minimum=self.minimum.value(),
                                            mode=self.generation_mode.currentData(), ornaments=self.ornaments.currentData(),
                                            overrides=self.source_overrides, bpm=self.bpm.value(),
                                            shift=None if self.auto_shift.isChecked() else self.shift.value(),
                                            quantize=self.grid.currentData() or (4 if self.rhythm_mode.currentData() == 'strict' else 0), grid_offset_ms=self.grid_offset.value(),
                                            rhythm=self.rhythm_mode.currentData(),
                                            use_detected_beats=self.auto_bpm.isChecked(), author=self.song.artists,
                                            instrument=self.instrument.currentData())
            if self.auto_shift.isChecked():
                self.shift.blockSignals(True)
                self.shift.setValue(stats['shift'])
                self.shift.blockSignals(False)
            if self.cover:
                image = QImage.fromData(self.cover)
                if not image.isNull():
                    self.score.root['cover'] = encode_cover(image)
            self.saved = False
        except ValueError as exc:
            self.score = None
            self.statistics.setText('没有符合当前过滤条件的音符')
            self.failed(str(exc))
        self.render_result()
        self.persist_draft()
        self.update_output()

    def visible_score(self):
        if self.score is None:
            return None
        score = copy.copy(self.score)
        score.notes = [n for n in self.score.notes if
                       (self.show_accompaniment.isChecked() if n.extra.get('role') == 'accompaniment'
                        else self.show_melody.isChecked())]
        return score

    def layers_changed(self, *_):
        if self.restoring or not hasattr(self, 'roll'):
            return
        self.stop_preview()
        self.render_result()
        self.persist_draft()

    def section_selected(self, *_):
        section = self.section_list.currentData()
        if section and not self.restoring:
            self.roll_seek(section['start_ms'])

    def section_source_menu(self):
        if not self.section_list.currentData() or self.busy:
            return
        menu = QMenu(self)
        for i in range(self.source_choice.count()):
            action = menu.addAction(self.source_choice.itemText(i))
            action.setData(i)
        chosen = menu.exec(self.apply_source.mapToGlobal(self.apply_source.rect().bottomLeft()))
        if chosen is not None:
            self.source_choice.setCurrentIndex(chosen.data())
            self.apply_section_source()

    def apply_section_source(self):
        section = self.section_list.currentData()
        if not section or self.busy or isinstance(self.result, MidiResult):
            return
        self.flush_remap()
        if not self.persist_draft():
            return
        source = self.source_choice.currentData()
        start, end = section['start_ms'], section['end_ms']
        self.source_overrides = [o for o in self.source_overrides if o['end_ms'] <= start or o['start_ms'] >= end]
        if source != 'auto':
            self.source_overrides.append(dict(start_ms=start, end_ms=end, source=source))
        self.remap()

    def render_result(self):
        duration = self.result.duration_ms if self.result else 0
        self.roll.set_score(self.visible_score(), duration)
        self.section_list.blockSignals(True)
        selected = self.section_list.currentIndex()
        self.section_list.clear()
        info = self.score.root.get('transcription', {}) if self.score else {}
        if self.score:
            notes = self.score.notes
            self.statistics.setText(
                f"{len(notes)} 个音符 · 移调 {info.get('shift', 0):+d} 半音\n"
                f"伴奏 {sum(n.extra.get('role') == 'accompaniment' for n in notes)} 个 · "
                f"待修正 {sum(s['needs_review'] for s in info.get('sections', []))} 段\n"
                f"半音近似 {sum(bool(n.extra.get('approximated')) for n in notes)} 个 · "
                f"映射待核对 {sum(bool(n.extra.get('mappingReview')) for n in notes)} 处")
        for section in info.get('sections', []):
            title = SOURCE_NAMES.get(section['source'], section['source'])
            if info.get('mode') == 'rich_chords':
                title = {'rest': '旋律休止', 'uncertain': '旋律待修正'}.get(section['source'], title)
            self.section_list.addItem(f"{clock_text(section['start_ms'] / 1000)}—{clock_text(section['end_ms'] / 1000)} · {title}", section)
        self.section_list.setCurrentIndex(max(0, min(selected, self.section_list.count() - 1)))
        self.section_list.blockSignals(False)
        self.statistics.setToolTip('\n'.join([
            '映射待核对表示光遇音域或半音改编限制；旋律待修正表示缺少可靠旋律证据。有独立证据的伴奏仍可保留。',
            *info.get('warnings', []),
            *[f"{clock_text(s['start_ms']/1000)}—{clock_text(s['end_ms']/1000)} · {s['reason']}"
              for s in info.get('harmony_review', [])[:40]]]))
        self.seek.setRange(0, round(max(duration, self.score.duration_ms if self.score else 0)))
        self.context_label.setText(f'对应原曲 {clock_text(self.result_start())} · 只读预览，点击定位'
                                  + (' · 人工编辑版本' if self.edited else '')
                                  + (' · ' + info['warnings'][0] if info.get('warnings') else ''))
        self.result_title.setText('曲谱预览 · ' + ('已编辑' if self.edited else '已生成' if self.score and self.score.notes else '待修正'))
        self.update_roll_grid()

    def update_roll_grid(self, *_):
        if not hasattr(self, 'roll'):
            return
        self.roll.subdivision = self.display_grid.currentData()
        self.roll.grid_offset_ms = self.grid_offset.value() if hasattr(self, 'grid_offset') else 0
        self.roll.viewport().update()

    def persist_draft(self):
        if not self.result or not self.song:
            return True
        try:
            self.draft_id = self.drafts.save(self.draft_id, self.song, self.result, self.score, self.parameters(),
                                            edited=self.edited, saved=self.saved, media=self.media_dir,
                                            source=self.source_dir, title=self.draft_title)
            self.draft_status.setText('草稿已自动保存' + (' · 已保存到曲库' if self.saved else ''))
            directory = self.drafts.path(self.draft_id)
            if (directory / 'original.wav').exists() or (directory / 'vocals.wav').exists():
                self.media_dir = directory
            if (directory / 'source.audio').exists() or (directory / 'source.mid').exists():
                self.source_dir = directory
            self.refresh_history()
            self.drafts.prune_audio(protected=(self.draft_id,))
            self.draft_write_failed = False
            return True
        except (OSError, ValueError) as exc:
            self.draft_write_failed = True
            self.draft_status.setText('草稿保存失败：' + str(exc))
            return False

    def refresh_history(self, *_):
        query = self.history_search.text().casefold().strip()
        self.history_list.clear()
        for entry in self.drafts.entries():
            song = entry['song']
            title = entry.get('title', song['name'])
            if query and query not in (title + song['name'] + song['artists']).casefold():
                continue
            start = entry['metadata'].get('source_start', 0)
            end = start + entry['metadata'].get('requested_length', 0)
            state = '已保存到曲库' if entry['saved'] else '已编辑' if entry['edited'] else '已生成'
            from datetime import datetime
            date = datetime.fromtimestamp(entry['created']).strftime('%m-%d %H:%M')
            item = QListWidgetItem(f"{title}\n{clock_text(start)}—{clock_text(end)}\n{state} · {date}")
            item.setData(Qt.ItemDataRole.UserRole, entry['id'])
            item.setToolTip(song['name'] + ' · ' + song['artists'] + '\n首次生成：' +
                            datetime.fromtimestamp(entry['created']).strftime('%Y-%m-%d %H:%M:%S') +
                            ('（旧草稿推定时间）' if entry.get('created_estimated') else ''))
            score_data = entry.get('score')
            if score_data:
                cover = score_data[0].get('cover', '')
                try:
                    pixmap = QPixmap()
                    if pixmap.loadFromData(base64.b64decode(cover)):
                        item.setIcon(QIcon(pixmap))
                except (ValueError, TypeError):
                    pass
            item.setSizeHint(QSize(190, 76))
            self.history_list.addItem(item)
            if entry['id'] == self.draft_id:
                self.history_list.setCurrentItem(item)

    def open_draft(self, item):
        ident = item.data(Qt.ItemDataRole.UserRole)
        if ident == self.draft_id or self.busy:
            return
        self.flush_remap()
        if not self.persist_draft():
            return
        try:
            data, song, result, score = self.drafts.load(ident)
        except (OSError, ValueError, KeyError) as exc:
            self.failed('草稿无法读取：' + str(exc))
            return
        self.discard_result()
        self.release_source()
        self.restoring = True
        self.draft_id, self.song, self.result, self.score = ident, song, result, score
        self.edited, self.saved = data['edited'], data['saved']
        self.draft_title = data.get('title')
        directory = self.drafts.path(ident)
        self.media_dir = self.source_dir = directory
        self.wave_data = load_waveform(directory)
        self.audio = None
        if self.wave_data and ((directory / 'source.audio').exists() or (directory / 'source.mid').exists()):
            origin = self.wave_data['origin']
            trial = self.wave_data.get('trial', False)
            self.audio = NeteaseAudio(song.id, '', self.wave_data['full_duration'] * 1000,
                                     origin if trial else None,
                                     origin + self.wave_data['duration'] if trial else None)
        if self.wave_data:
            self.waveform.set_data(self.wave_data)
        else:
            self.waveform.data = None
        params = data['params']
        self.backend_choice.setCurrentIndex(max(0, self.backend_choice.findData(result.metadata.get('backend', 'muscriptor'))))
        self.model_choice.setCurrentIndex(max(0, self.model_choice.findData(result.metadata.get('model', params.get('model', 'small')))))
        self.device_choice.setCurrentIndex(max(0, self.device_choice.findData(params.get('device', 'auto'))))
        self.source_overrides = params.get('overrides', [])
        for widget, key in ((self.threshold, 'threshold'), (self.minimum, 'minimum'), (self.shift, 'shift'),
                            (self.bpm, 'bpm'), (self.grid_offset, 'grid_offset')):
            widget.setValue(params.get(key, 0))
        for widget, key in ((self.auto_shift, 'auto_shift'), (self.auto_bpm, 'auto_bpm')):
            widget.setChecked(params.get(key, True))
        for widget, key in ((self.grid, 'grid'), (self.display_grid, 'display_grid'), (self.instrument, 'instrument')):
            widget.setCurrentIndex(max(0, widget.findData(params.get(key))))
        for widget, key, default in ((self.generation_mode, 'mode', 'simple'), (self.rhythm_mode, 'rhythm', 'original'),
                                     (self.ornaments, 'ornaments', 'simplify')):
            widget.setCurrentIndex(max(0, widget.findData(params.get(key, default))))
        self.show_melody.setChecked(params.get('show_melody', True))
        self.show_accompaniment.setChecked(params.get('show_accompaniment', True))
        self.cover = None
        if score and score.root.get('cover'):
            try:
                self.cover = base64.b64decode(score.root['cover'])
            except ValueError:
                pass
        self.set_selection(self.result_start(), self.result_start() + result.metadata.get('requested_length', result.duration_ms / 1000))
        self.audition_source = False
        self.preview_mode.setCurrentIndex(2 if score else 0)
        self.restoring = False
        self.update_song_card()
        self.render_result()
        self.roll.fit()
        self.set_busy(False)
        self.draft_status.setText('草稿已恢复' + (' · 已保存到曲库' if self.saved else ''))
        self.status.setText('草稿与源文件已恢复，可直接编辑或重新处理。' if self.audio else
                            '草稿已恢复；此旧草稿未保留源文件，请恢复音频。')
        self.refresh_history()

    def delete_draft(self):
        item = self.history_list.currentItem()
        if not item or self.busy:
            return
        ident = item.data(Qt.ItemDataRole.UserRole)
        if QMessageBox.question(self, '删除草稿', '删除此草稿及其缓存？曲库中的歌曲会保留。') != QMessageBox.StandardButton.Yes:
            return
        try:
            if ident == self.draft_id:
                self.discard_result()
                self.release_source()
                self.song = self.audio = None
                self.waveform.data = None
                self.waveform.update()
                self.song_info.setText('选择歌曲，试听并标记需要扒谱的片段。')
                self.cover_label.clear()
            self.drafts.delete(ident)
            self.refresh_history()
            self.set_busy(False)
        except OSError as exc:
            self.failed(str(exc))

    def clear_audio_cache(self, confirm=True):
        if self.busy:
            return
        if confirm and QMessageBox.question(self, '清理音频缓存', '清理下载和试听缓存？草稿源文件和曲谱均会保留。') != QMessageBox.StandardButton.Yes:
            return
        self.flush_remap()
        if not self.persist_draft():
            return
        self.stop_preview()
        self.media.setSource(QUrl())
        if self.task:
            self.task.close()
            self.task = None
        try:
            DownloadCache(self.host.store.directory).prune(clear=True)
            self.drafts.prune_audio(budget=0)
            from ..transcription.hybrid import clear_hybrid_audio
            clear_hybrid_audio(self.host.store.directory)
            self.update_output()
            self.status.setText('下载和试听缓存已清理，草稿源文件与曲谱仍保留。')
        except OSError as exc:
            self.failed(str(exc))

    def result_start(self):
        return self.result.metadata.get('source_start', 0) if self.result else self.start_time.value()

    def play_duration(self):
        if self.result and not self.audition_source:
            return max(self.result.duration_ms, self.score.duration_ms if self.score else 0)
        return self.length.value() * 1000

    def stop_preview(self, *_):
        self.player.stop()
        self.media.stop()
        self.pending_seek = None
        self.preview.setText('播放')

    def update_volume(self, value):
        self.audio_output.setVolume(value / 100)
        if hasattr(self, 'local_mixer'):
            self.local_mixer.volume = value / 100

    def tone_changed(self, *_):
        if self.restoring:
            return
        self.stop_preview()
        if self.generation_mode.currentData() == 'chords' and self.instrument.currentData() != 'Piano' and not self.edited:
            self.generation_mode.blockSignals(True)
            self.generation_mode.setCurrentIndex(self.generation_mode.findData('auto'))
            self.generation_mode.blockSignals(False)
        self.update_output()
        # Merely auditioning another timbre must never destroy an edited score.
        # An explicit generation-mode change forks the durable edited version.
        if self.edited:
            self.persist_draft()
            self.status.setText('已切换试听音色；人工编辑谱保持原样。调整生成模式可生成新的编配草稿。')
        else:
            self.schedule_remap()

    def toggle_preview(self):
        if self.busy:
            return
        self.flush_remap()
        if self.player.state == State.PLAYING:
            self.player.pause()
            self.preview.setText('播放')
            return
        if self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media.pause()
            self.preview.setText('播放')
            return
        self.play_preview()

    def play_preview(self):
        self.host.stop()
        self.host.release_manual()
        position = self.seek.value()
        if position >= self.play_duration() - 20:
            position = 0
            self.seek.setValue(0)
        if self.preview_mode.currentIndex() == 2:
            if not self.score or not self.visible_score().notes:
                self.failed('当前可见声部没有可试听的音符；请检查待修正段落或声部开关')
                return
            from ..audio import Mixer, QtAudioOutput
            try:
                if not hasattr(self, 'local_mixer'):
                    self.local_mixer = Mixer()
                    self.local_audio = None
                if not self.host.no_audio:
                    self.local_mixer.prepare(self.instrument.currentData())
                    if self.local_audio is None:
                        self.local_audio = QtAudioOutput(self.local_mixer)
                self.local_mixer.volume = self.volume.value() / 100
                self.player.load(self.visible_score())
                self.player.timeline.duration_ms = self.play_duration()
                self.player.seek(position)
                self.player.play(self.local_mixer)
            except (OSError, ValueError, RuntimeError) as exc:
                self.failed(str(exc))
                return
        else:
            mode = self.preview_mode.currentIndex()
            path = None
            self.media_offset = 0
            if self.result and not self.audition_source and self.media_dir:
                candidate = self.media_dir / {0: 'original.wav', 1: 'vocals.wav', 3: 'other.wav', 4: 'bass.wav', 5: 'background.wav'}[mode]
                if candidate.exists():
                    path = candidate
            if path is None and mode == 0 and self.source_dir and (self.source_dir / 'source.audio').exists():
                path = self.source_dir / 'source.audio'
                origin = self.wave_data['origin'] if self.wave_data else 0
                self.media_offset = ((self.start_time.value() if self.audition_source or not self.result else self.result_start()) - origin) * 1000
            if path is None:
                self.failed('音频缓存不可用。原曲可通过恢复音频重新获取；分离人声需重新识别。')
                return
            url = QUrl.fromLocalFile(str(path.resolve()))
            self.pending_seek = round(position + self.media_offset)
            if self.media.source() != url:
                self.media.setSource(url)
            self.media.setPosition(self.pending_seek)
            self.media.play()
        self.preview.setText('暂停')

    def media_ready(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) and self.pending_seek is not None:
            self.media.setPosition(self.pending_seek)
            self.pending_seek = None
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.playback_finished()

    def playback_finished(self):
        self.stop_preview()
        if self.loop.isChecked() and not self.busy:
            self.seek.setValue(0)
            self.play_preview()

    def switch_preview(self, *_):
        if self.restoring:
            return
        was_playing = self.player.state == State.PLAYING or self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        position = self.seek.value()
        if self.audition_source and self.result:
            position = max(0, position + (self.start_time.value() - self.result_start()) * 1000)
        self.stop_preview()
        self.audition_source = False
        self.seek.setRange(0, round(self.play_duration()))
        self.seek.setValue(round(position))
        if was_playing:
            self.play_preview()

    def seek_preview(self, value):
        value = max(0, min(round(self.play_duration()), round(value)))
        self.seek.setValue(value)
        if self.preview_mode.currentIndex() == 2:
            self.player.seek(value)
        else:
            self.pending_seek = round(value + self.media_offset)
            self.media.setPosition(self.pending_seek)

    def wave_seek(self, absolute):
        if isinstance(self.result, MidiResult):
            self.roll_seek((absolute - self.result_start()) * 1000)
            return
        self.stop_preview()
        self.preview_mode.blockSignals(True)
        self.preview_mode.setCurrentIndex(0)
        self.preview_mode.blockSignals(False)
        self.audition_source = True
        self.seek.setRange(0, round(self.length.value() * 1000))
        self.seek.setValue(round(max(0, min(self.length.value(), absolute - self.start_time.value())) * 1000))

    def roll_seek(self, value):
        if self.audition_source:
            self.stop_preview()
            self.audition_source = False
            self.seek.setRange(0, round(self.play_duration()))
        self.seek_preview(value)

    def edit_score(self):
        self.flush_remap()
        if not self.score:
            return
        self.stop_preview()
        self.host.stop()
        self.host.release_manual()
        editor = EditorDialog(self.host, self.score, draft_mode=True)
        editor.exec()
        if editor.saved:
            self.score = copy.deepcopy(editor.score)
            self.score.path = None
            self.draft_title = self.score.name
            self.edited, self.saved = True, False
            self.bpm.blockSignals(True)
            self.bpm.setValue(self.score.bpm)
            self.bpm.blockSignals(False)
            self.statistics.setText(f'已编辑 · {len(self.score.notes)} 个音符\n再次调整生成参数会创建新版本。')
            self.render_result()
            self.persist_draft()
            self.status.setText('编辑已回写草稿，可保存到曲库。')
            self.update_output()

    def refine_region(self):
        self.flush_remap()
        if not self.persist_draft():
            return
        self.stop_preview()
        from .refinement import RefinementDialog
        dialog = RefinementDialog(self)
        dialog.exec()
        if dialog.child_id:
            self.refresh_history()
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, dialog.child_id)
            self.open_draft(item)
            self.status.setText('已接受精修并另存新版本；原草稿完整保留。')

    def save_score(self):
        self.flush_remap()
        if not self.score:
            return
        try:
            saved = self.host.store.add_score(copy.deepcopy(self.score))
            self.host.show_page(0)
            self.host.refresh()
            self.saved = True
            self.persist_draft()
            self.update_output()
            self.status.setText('已保存到曲库：' + saved.path.name)
        except (OSError, ValueError) as exc:
            self.failed(str(exc))

    def save_midi(self):
        self.flush_remap()
        if not self.result:
            return
        path, _ = QFileDialog.getSaveFileName(self, '导出识别后的原始音高与节奏', self.song.name + '.mid', 'MIDI (*.mid)')
        if path:
            try:
                if isinstance(self.result, EventResult):
                    self.result.export_midi(Path(path))
                    self.status.setText('复合多轨 MIDI 已导出（原稿＋已接受局部事件；不含光遇映射）'
                                        if self.result.metadata.get('refinement') else
                                        '原始多轨 MIDI 已导出（模型音符事件，固定力度；不含光遇映射）')
                    return
                if isinstance(self.result, MidiResult):
                    notes = self.result.notes()
                else:
                    notes, _ = arrange(self.result, self.threshold.value(), self.minimum.value(),
                                       generation_mode(self.generation_mode.currentData(), self.instrument.currentData()),
                                       self.ornaments.currentData(), self.source_overrides)
                export_midi([n for n in notes if not n.generated], Path(path),
                            self.bpm.value(), self.song.name)
                self.status.setText('原始旋律 MIDI 已导出（不含光遇映射和人工编辑）')
            except (OSError, ValueError) as exc:
                self.failed(str(exc))

    def closeEvent(self, event):
        self.flush_remap()
        if not self.persist_draft():
            choice = QMessageBox.question(self, '草稿未保存', '草稿保存失败，仍要关闭吗？',
                                          QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                          QMessageBox.StandardButton.Cancel)
            if choice != QMessageBox.StandardButton.Discard:
                event.ignore()
                return
        self.install_cancelled.set()
        self.device_status.stop()
        self.stop_preview()
        self.media.setSource(QUrl())
        self.release_source()
        if self.task:
            self.task.close()
        if getattr(self, 'local_audio', None):
            self.local_audio.close()
        self.timer.stop()
        self.deactivate()
        super().reject()
        event.accept()

    def reject(self):
        self.close()
