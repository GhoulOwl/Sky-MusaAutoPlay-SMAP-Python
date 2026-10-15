"""Modal, recoverable candidate review; the parent workbench remains unchanged."""
from __future__ import annotations

import copy
import shutil
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import soundfile as sf
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..audio import SAMPLE_RATE
from ..netease import NeteaseAudio
from ..transcription.audition import render_window
from ..transcription.engine import select_engine
from ..transcription.refinement import (
    CandidateStore,
    Region,
    fingerprint,
    frozen_rhythm,
    make_candidate,
    make_safe_candidate,
    note_diff,
    reference_groups,
    search_context,
)
from ..transcription.task import TranscriptionTask
from . import style
from .transcription_widgets import PreviewRoll, clock_text


class RegionRuler(QWidget):
    selected = Signal(float, float)

    def __init__(self, duration, parent=None):
        super().__init__(parent)
        self.duration, self.start, self.end = duration, 0., min(8000., duration)
        self.groups, self.anchor = [], None
        self.setMinimumHeight(65)
        self.setAccessibleName('拖动选择精修参考小节或时间范围')

    def at(self, x):
        value = min(self.duration, max(0, (x-10)/max(1, self.width()-20)*self.duration))
        return min([0, *self.groups, self.duration], key=lambda t: abs(t-value)) if self.groups else value

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(style.colors['panel']))
        def x(t):
            return 10+t/self.duration*(self.width()-20)
        p.fillRect(QRectF(x(self.start), 20, max(1, x(self.end)-x(self.start)), 40), QColor(style.colors['accent']))
        p.setPen(QColor(style.colors['muted']))
        for t in self.groups:
            p.drawLine(QPointF(x(t), 20), QPointF(x(t), 60))
        for i in range(6):
            t = self.duration*i/5
            p.drawText(QRectF(max(0, min(self.width()-70, x(t)-20)), 0, 70, 20), clock_text(t/1000))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.anchor = self.at(event.position().x())

    def mouseMoveEvent(self, event):
        if self.anchor is not None:
            self.start, self.end = sorted((self.anchor, self.at(event.position().x())))
            self.update()

    def mouseReleaseEvent(self, event):
        if self.anchor is not None:
            a, b = sorted((self.anchor, self.at(event.position().x())))
            self.anchor = None
            if b > a:
                self.selected.emit(a, b)


class RefinementRoll(PreviewRoll):
    def __init__(self):
        super().__init__()
        self.bounds = (0, 0)
        self.requested = None
        self.added, self.removed = [], []

    def drawForeground(self, painter, rect):
        scale = self.pixels_per_beat/self.score.ms_per_beat
        height = 15*self.row_height
        left, right = [t*scale for t in self.bounds]
        shade = QColor('#10121a')
        shade.setAlpha(85)
        painter.fillRect(QRectF(rect.left(), 0, max(0, left-rect.left()), height), shade)
        painter.fillRect(QRectF(max(right, rect.left()), 0, max(0, rect.right()-max(right, rect.left())), height), shade)
        painter.setPen(QPen(QColor('#d7a64b'), 2))
        for x in (left, right):
            painter.drawLine(QPointF(x, 0), QPointF(x, height))
        if self.requested and tuple(self.requested) != self.bounds:
            painter.setPen(QPen(QColor('#a9b8ca'), 1.5, Qt.PenStyle.DotLine))
            for t in self.requested:
                painter.drawLine(QPointF(t*scale, 0), QPointF(t*scale, height))
        for notes, color, line in ((self.added, '#41bd8c', Qt.PenStyle.SolidLine),
                                   (self.removed, '#ed859a', Qt.PenStyle.DashLine)):
            painter.setPen(QPen(QColor(color), 2, line))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for n in notes:
                painter.drawRect(QRectF(n.time_ms*scale, (14-n.key)*self.row_height+3,
                                       max(3, n.duration_ms*scale), self.row_height-6))
        super().drawForeground(painter, rect)


class RefinementDialog(QDialog):
    completed = Signal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self.workbench = parent
        self.directory = parent.host.store.directory
        self.parent_id = parent.draft_id
        self.original, self.baseline = copy.deepcopy(parent.result), copy.deepcopy(parent.score)
        self.params = copy.deepcopy(parent.parameters())
        self.store = CandidateStore(self.directory)
        recovery_error = ''
        try:
            self.candidate = self.store.pending(self.parent_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.candidate = None
            recovery_error = f'待处理候选无法读取，已保留其文件；可重新生成。{exc}'
        self.child_id = None
        self.task, self.thread = None, None
        self.cancelled = threading.Event()
        self.closing = False
        self.preview_files = {}
        self.preview_start, self.preview_end = 0, 0
        self.pending_position, self.pending_play = 0, False
        self.temp = Path(tempfile.mkdtemp(prefix='autosky-refine-preview-'))
        self.setWindowTitle('小节／片段精修 · 原版与候选')
        self.resize(1080, 760)
        root = QVBoxLayout(self)
        intro = QLabel('参考小节仅按拍点分组，不代表已识别重拍。选择 2–4 组或不超过 30 秒；接受后另存新版本。'
                       + (' 当前草稿含人工编辑，选区内的编辑音符将参与替换，请先试听。' if parent.edited else ''))
        intro.setWordWrap(True)
        root.addWidget(intro)
        row = QHBoxLayout()
        self.group_count = QSpinBox()
        self.group_count.setRange(2, 12)
        self.group_count.setValue(4)
        self.origin = QDoubleSpinBox()
        self.origin.setRange(0, self.original.duration_ms/1000)
        self.origin.setDecimals(3)
        self.reference_mode = QCheckBox('参考小节')
        self.reference_mode.setChecked(self.original.analysis.get('beat_confidence', 0) >= .55)
        self.left, self.right = QDoubleSpinBox(), QDoubleSpinBox()
        for box in (self.left, self.right):
            box.setRange(0, self.original.duration_ms/1000)
            box.setDecimals(3)
        self.right.setValue(min(8, self.original.duration_ms/1000))
        for text, widget in (('每组拍数', self.group_count), ('分组起点 / 秒', self.origin),
                             ('入点 / 秒', self.left), ('出点 / 秒', self.right)):
            row.addWidget(QLabel(text))
            row.addWidget(widget)
        row.addWidget(self.reference_mode)
        root.addLayout(row)
        self.ruler = RegionRuler(self.original.duration_ms)
        root.addWidget(self.ruler)
        self.ruler.selected.connect(self.set_range)
        for box in (self.left, self.right, self.group_count, self.origin):
            box.valueChanged.connect(self.range_changed)
        self.reference_mode.toggled.connect(self.range_changed)
        self.boundary_info = QLabel()
        self.boundary_info.setWordWrap(True)
        self.boundary_info.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.boundary_info)
        controls = QHBoxLayout()
        self.model = QComboBox()
        self.model.addItems(['small', 'medium'])
        self.model.setCurrentText(self.original.metadata.get('model', 'small'))
        self.generate = QPushButton('生成精修候选')
        self.generate.clicked.connect(self.start)
        self.cancel = QPushButton('取消任务')
        self.cancel.clicked.connect(self.cancel_job)
        self.cancel.setEnabled(False)
        controls.addWidget(QLabel('精修模型'))
        controls.addWidget(self.model)
        self.safe_boundaries = QCheckBox('寻找安全边界（向外最多各 4 秒）')
        self.safe_boundaries.setChecked(True)
        self.safe_boundaries.setToolTip('保留完整请求片段；核心总长仍不超过 30 秒。生成后展示实际范围，由您试听接受。')
        self.safe_boundaries.toggled.connect(self.range_changed)
        controls.addWidget(self.safe_boundaries)
        controls.addWidget(self.generate)
        controls.addWidget(self.cancel)
        controls.addStretch()
        root.addLayout(controls)
        self.notice = QLabel('精修只替换明确选区；跨界持续音、人工休止和选区外谱面受到保护。')
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.notice)
        self.roll = RefinementRoll()
        self.roll.set_score(self.baseline, self.original.duration_ms)
        root.addWidget(self.roll, 1)
        self.diff_label = QLabel('金线：实际精修范围；灰点线：原请求范围。绿框：候选独有；红虚线：原版独有。时值／音高变化计为一删一增。')
        self.diff_label.setWordWrap(True)
        root.addWidget(self.diff_label)
        review = QHBoxLayout()
        self.version = QComboBox()
        self.version.addItems(['原版', '候选', '原曲'])
        self.version.currentIndexChanged.connect(self.switch_version)
        self.only_lead = QCheckBox('仅主旋律')
        self.only_lead.setToolTip('仅影响光遇谱面试听；原曲始终为原始混音。')
        self.context = QCheckBox('含前后衔接')
        self.context.setChecked(True)
        self.only_lead.toggled.connect(self.clear_audio)
        self.only_lead.toggled.connect(self.show_score)
        self.context.toggled.connect(self.clear_audio)
        self.loop = QCheckBox('循环')
        self.loop.setChecked(True)
        self.play = QPushButton('准备并播放')
        self.play.clicked.connect(self.toggle_play)
        self.diff = QCheckBox('显示差异')
        self.diff.setChecked(True)
        self.diff.toggled.connect(self.show_score)
        for widget in (self.version, self.only_lead, self.context, self.loop, self.diff, self.play):
            review.addWidget(widget)
        root.addLayout(review)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.sliderMoved.connect(lambda t: self.media.setPosition(t))
        self.roll.seek_requested.connect(lambda t: self.media.setPosition(round(max(0, min(
            self.preview_end-self.preview_start, t-self.preview_start)))))
        root.addWidget(self.seek)
        self.clock = QLabel('')
        root.addWidget(self.clock)
        bottom = QHBoxLayout()
        self.accept_button = QPushButton('接受为新版本')
        self.accept_button.clicked.connect(self.accept_candidate)
        self.discard_button = QPushButton('丢弃候选')
        self.discard_button.clicked.connect(self.discard)
        close = QPushButton('关闭（保留待处理候选）')
        close.clicked.connect(self.close)
        bottom.addWidget(self.accept_button)
        bottom.addWidget(self.discard_button)
        bottom.addStretch()
        bottom.addWidget(close)
        root.addLayout(bottom)
        self.media = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.output.setVolume(parent.volume.value()/100)
        self.media.setAudioOutput(self.output)
        self.media.positionChanged.connect(self.position_changed)
        self.media.playbackStateChanged.connect(lambda state: self.play.setText(
            '暂停' if state == QMediaPlayer.PlaybackState.PlayingState else '播放'))
        self.media.mediaStatusChanged.connect(self.media_status)
        self.media.errorOccurred.connect(lambda *_: self.notice.setText('试听音频无法播放，请重新准备试听'))
        self.completed.connect(self.job_finished)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.progress)
        self.timer.start(100)
        if self.candidate:
            self.set_range(self.candidate.region.start, self.candidate.region.end)
        self.range_changed()
        self.update_state()
        if recovery_error:
            self.notice.setText(recovery_error)

    def set_range(self, left, right):
        self.left.setValue(left/1000)
        self.right.setValue(right/1000)

    def range_changed(self, *_):
        self.ruler.start, self.ruler.end = self.left.value()*1000, self.right.value()*1000
        self.ruler.groups = reference_groups(self.original, self.group_count.value(), self.origin.value()*1000) if self.reference_mode.isChecked() else []
        self.ruler.update()
        if hasattr(self, 'preview_files') and not self.candidate:
            self.clear_audio()
        if hasattr(self, 'roll'):
            self.show_score()
        if hasattr(self, 'boundary_info'):
            try:
                r = self.region()
                search = self.candidate.diagnostics.get('boundary_search') if self.candidate else None
                if search:
                    a, b = search['requested']
                    text = f'原请求 {a/1000:.3f}–{b/1000:.3f} 秒 → 实际精修 {r.start/1000:.3f}–{r.end/1000:.3f} 秒。'
                    text += ' 区外和跨界完整音符继续保护。' if search['safe'] else ' 尚未找到通过全部保护检查的边界，仍禁止接受。'
                else:
                    text = f'请求 {r.start/1000:.3f}–{r.end/1000:.3f} 秒；识别上下文 {r.context_start/1000:.3f}–{r.context_end/1000:.3f} 秒。'
                    if hasattr(self, 'safe_boundaries') and self.safe_boundaries.isChecked():
                        text += ' 安全范围可向外各扩至 4 秒，总长最多 30 秒；接受前将显示实际范围。'
                self.boundary_info.setText(text)
            except ValueError as exc:
                self.boundary_info.setText(str(exc))

    def region(self):
        if self.candidate:
            return self.candidate.region
        requested = Region.create(self.original, self.left.value()*1000, self.right.value()*1000, self.group_count.value())
        return search_context(self.original, requested) if hasattr(self, 'safe_boundaries') and self.safe_boundaries.isChecked() else requested

    def update_state(self):
        busy = self.thread is not None and self.thread.is_alive()
        pending = self.candidate is not None
        for w in (self.ruler, self.left, self.right, self.group_count, self.origin, self.reference_mode, self.model, self.generate, self.safe_boundaries):
            w.setEnabled(not busy and not pending)
        self.cancel.setEnabled(busy)
        stale = pending and self.candidate.baseline != fingerprint(self.original, self.baseline, self.params)
        self.accept_button.setEnabled(pending and not busy and not stale and not self.candidate.conflicts)
        self.discard_button.setEnabled(pending and not busy)
        self.play.setEnabled(not busy)
        self.context.setEnabled(not busy)
        self.only_lead.setEnabled(not busy)
        self.version.setEnabled(not busy)
        self.version.model().item(1).setEnabled(pending)
        if not pending and self.version.currentIndex() == 1:
            self.version.setCurrentIndex(0)
        if pending:
            d = self.candidate.diagnostics
            details = f"选区内音符 {d['before_notes']} → {d['after_notes']}；区外／跨界保护 {d['protected_notes']} 个。"
            delta = note_diff(self.baseline, self.candidate.score)
            details += f" 演奏差异：删除 {delta['removed']}，新增 {delta['added']}。"
            if stale:
                details += ' 原稿已改变，候选过期；请丢弃后重新生成。'
            elif self.candidate.conflicts:
                details += ' 无法安全接受：'+'；'.join(self.candidate.conflicts[:3])
            elif d['unchanged']:
                details += ' 候选与原版相同，不代表识别改善。'
            else:
                details += ' 请对照原曲试听；音符增减不代表准确率提高。'
            self.notice.setText(details)
        self.show_score()

    def start(self):
        try:
            region = self.region()
            engine = select_engine(self.directory, self.model.currentText(), self.params.get('device', 'auto'))
            source = self.workbench.source_dir/'source.audio'
            info = sf.info(source)
            origin = self.original.metadata.get('source_origin', 0)
            start = self.original.metadata.get('source_start', 0)+region.context_start/1000
            audio = NeteaseAudio(self.workbench.song.id, '', (origin+info.duration)*1000,
                                origin if origin else None, origin+info.duration if origin else None)
            self.task = TranscriptionTask(self.directory, SimpleNamespace(), self.workbench.song, audio,
                engine, start, (region.context_end-region.context_start)/1000, source=source,
                source_kind='local_audio', rhythm_reference=frozen_rhythm(self.original, region))
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            self.notice.setText(str(exc))
            return
        self.media.stop()
        self.cancelled.clear()
        self.notice.setText('正在生成局部候选；原草稿保持原样。')
        task = self.task
        automatic = self.safe_boundaries.isChecked()
        def work():
            try:
                task.start()
                task.thread.join()
                if self.cancelled.is_set():
                    raise ValueError('精修已取消，原草稿未改变')
                if task.result is None:
                    errors = [e.get('message') for e in list(task.events.queue) if e['stage'] in ('error', 'cancelled')]
                    raise ValueError(errors[-1] if errors else '精修识别失败')
                task.emit('candidate', message='检查边界、休止与光遇编配约束')
                if automatic:
                    candidate = make_safe_candidate(self.parent_id, self.original, self.baseline, self.params,
                                                    task.result, region, self.cancelled.is_set)
                else:
                    candidate = make_candidate(self.parent_id, self.original, self.baseline, self.params, task.result, region)
                if self.cancelled.is_set():
                    raise ValueError('精修已取消，原草稿未改变')
                self.store.save(candidate, task.result)
                message = ('candidate', candidate)
            except Exception as exc:
                message = ('error', str(exc))
            finally:
                task.close()
            self.completed.emit(message)
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        self.update_state()

    def progress(self):
        if self.task and self.thread and self.thread.is_alive():
            events = list(self.task.events.queue)
            if events:
                event = events[-1]
                self.notice.setText(event.get('message') or f"局部识别 · {event.get('stage')} · {event.get('progress', 0)}%")

    def cancel_job(self):
        self.cancelled.set()
        if self.task:
            self.task.cancel()

    def job_finished(self, message):
        if self.closing:
            shutil.rmtree(self.temp, ignore_errors=True)
            return
        self.thread = None
        self.task = None
        kind, value = message
        if kind == 'candidate':
            self.candidate = value
            self.set_range(value.region.start, value.region.end)
            self.range_changed()
            self.clear_audio()
        elif kind == 'audio':
            self.preview_files = value
            self.switch_version()
        self.update_state()
        if kind == 'error':
            self.notice.setText(value)
        elif kind == 'audio':
            self.media.play()

    def show_score(self, *_):
        candidate = self.candidate.score if self.candidate and self.version.currentIndex() == 1 else self.baseline
        score = copy.deepcopy(candidate)
        if self.only_lead.isChecked():
            score.notes = [n for n in score.notes if n.extra.get('role') != 'accompaniment']
        self.roll.set_score(score, self.original.duration_ms)
        try:
            region = self.region()
            self.roll.bounds = (region.start, region.end)
            self.roll.requested = self.candidate.diagnostics.get('boundary_search', {}).get('requested') if self.candidate else None
            span = max(1000, region.context_end-region.context_start)
            self.roll.pixels_per_beat = max(2, min(400, (self.roll.viewport().width()-24)*score.ms_per_beat/span))
            self.roll.set_score(score, self.original.duration_ms)
            self.roll.centerOn((region.context_start+region.context_end)/2/score.ms_per_beat*self.roll.pixels_per_beat,
                               7.5*self.roll.row_height)
        except ValueError:
            pass  # A partially entered selection is validated before recognition/playback.
        self.roll.added, self.roll.removed = [], []
        if self.diff.isChecked() and self.candidate:
            def key(n):
                return n.key, n.time_ms, n.duration_ms, n.extra.get('role')
            old, new = {key(n) for n in self.baseline.notes}, {key(n) for n in self.candidate.score.notes}
            def visible(n):
                return not self.only_lead.isChecked() or n.extra.get('role') != 'accompaniment'
            self.roll.added = [n for n in self.candidate.score.notes if key(n) not in old and visible(n)]
            self.roll.removed = [n for n in self.baseline.notes if key(n) not in new and visible(n)]
        self.roll.viewport().update()

    def clear_audio(self, *_):
        if hasattr(self, 'media'):
            self.media.stop()
            self.media.setSource(QUrl())
        self.preview_files = {}

    def prepare_audio(self):
        region = self.region()
        a, b = (region.context_start, region.context_end) if self.context.isChecked() else (region.start, region.end)
        self.preview_start, self.preview_end = a, b
        self.seek.setRange(0, round(b-a))
        self.cancelled.clear()
        only_lead = self.only_lead.isChecked()
        source = self.workbench.source_dir/'source.audio'
        self.notice.setText('正在准备等音量对照，保留进入选区的钢琴音尾与持续音…')
        def work():
            try:
                files = {}
                for name, score in (('原版', self.baseline), ('候选', self.candidate.score if self.candidate else self.baseline)):
                    output = render_window(score, a, b, self.params.get('instrument', 'Piano'), only_lead, self.cancelled.is_set)
                    file = self.temp/(name+'.wav')
                    sf.write(file, output, SAMPLE_RATE, subtype='FLOAT')
                    files[name] = file
                with sf.SoundFile(source) as stream:
                    offset = self.original.metadata.get('source_start', 0)-self.original.metadata.get('source_origin', 0)+a/1000
                    stream.seek(round(offset*stream.samplerate))
                    reference = stream.read(round((b-a)/1000*stream.samplerate), dtype='float32', always_2d=True)
                    sf.write(self.temp/'source.wav', reference*.5, stream.samplerate, subtype='FLOAT')
                if self.cancelled.is_set():
                    raise ValueError('试听准备已取消')
                files['原曲'] = self.temp/'source.wav'
                self.completed.emit(('audio', files))
            except Exception as exc:
                self.completed.emit(('error', str(exc)))
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        self.update_state()

    def toggle_play(self):
        if self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media.pause()
            return
        if not self.preview_files:
            try:
                self.prepare_audio()
            except ValueError as exc:
                self.notice.setText(str(exc))
        else:
            if self.media.position() >= self.media.duration()-20:
                self.media.setPosition(0)
            self.media.play()

    def switch_version(self, *_):
        self.show_score()
        if not self.preview_files:
            return
        self.pending_position = self.media.position()
        self.pending_play = self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.media.stop()
        self.media.setSource(QUrl.fromLocalFile(str(self.preview_files[self.version.currentText()])))

    def media_status(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            if self.pending_position is not None:
                self.media.setPosition(self.pending_position)
                self.pending_position = None
                if self.pending_play:
                    self.media.play()
        elif status == QMediaPlayer.MediaStatus.EndOfMedia and self.loop.isChecked():
            self.media.setPosition(0)
            self.media.play()

    def position_changed(self, position):
        if not self.seek.isSliderDown():
            self.seek.setValue(position)
        absolute = self.preview_start+position
        self.roll.set_position(absolute)
        self.clock.setText(f'草稿 {clock_text(absolute/1000)} / {clock_text(self.preview_end/1000)}')
        self.play.setText('暂停' if self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState else '播放')

    def accept_candidate(self):
        try:
            self.child_id = self.store.accept(self.candidate)
        except (ValueError, OSError) as exc:
            self.notice.setText(str(exc))
            return
        self.close()

    def discard(self):
        if self.candidate:
            self.store.discard(self.candidate)
            self.candidate = None
        self.clear_audio()
        self.notice.setText('候选已丢弃，原草稿未改变。')
        self.update_state()
        self.range_changed()

    def reject(self):
        self.close()

    def closeEvent(self, event):
        self.closing = True
        self.cancel_job()
        self.media.stop()
        self.media.setSource(QUrl())
        self.timer.stop()
        if not self.thread or not self.thread.is_alive():
            shutil.rmtree(self.temp, ignore_errors=True)
        event.accept()
