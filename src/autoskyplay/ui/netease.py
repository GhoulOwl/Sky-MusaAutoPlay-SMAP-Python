"""Background QR login and explicit song/version selection."""

from __future__ import annotations

import base64

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..netease import NeteaseError
from .widgets import label


class ServiceDialog(QDialog):
    def __init__(self, parent, host):
        super().__init__(parent)
        self.host = host
        self.client = host.netease_client
        self.active = True
        self.generation = 0
        self.finished.connect(self.deactivate)

    def deactivate(self, _=None):
        self.active = False
        self.generation += 1

    def job(self, work, result, error=None, finished=None, priority=1):
        if not self.active:
            return
        generation = self.generation

        def guarded(callback):
            def invoke(value=None):
                if self.active and generation == self.generation:
                    if callback:
                        callback(value)

            return invoke

        def execute():
            if self.active and not self.host.closed and generation == self.generation:
                return work()
            return None

        self.host.run_job(
            execute,
            guarded(result),
            guarded(error or self.failed),
            lambda: guarded(finished)(),
            priority=priority,
        )

    def failed(self, message):
        self.status.setText(message)


class NeteaseLoginDialog(ServiceDialog):
    def __init__(self, host):
        super().__init__(host, host)
        self.setWindowTitle("网易云登录")
        self.setMinimumSize(360, 420)
        root = QVBoxLayout(self)
        root.addWidget(label("用网易云音乐 App 扫码登录", "heading"))
        self.qr = QLabel()
        self.qr.setFixedSize(280, 280)
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.qr, alignment=Qt.AlignmentFlag.AlignCenter)
        self.status = label("正在启动内置服务…")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status)
        root.addWidget(label("登录会话保存到项目目录的本地文件，重启后自动恢复。", "muted"))
        self.refresh = QPushButton("刷新二维码")
        self.refresh.clicked.connect(self.load_qr)
        root.addWidget(self.refresh)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        root.addWidget(cancel)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll)
        self.finished.connect(self.timer.stop)
        self.key = ""
        self.polling = False
        QTimer.singleShot(0, self.load_qr)

    def load_qr(self):
        if not self.active:
            return
        self.timer.stop()
        self.generation += 1
        self.key = ""
        self.polling = False
        self.qr.clear()
        self.refresh.setEnabled(False)
        self.status.setText("正在生成登录二维码…")

        def loaded(value):
            key, data = value
            pixmap = QPixmap()
            try:
                valid = pixmap.loadFromData(base64.b64decode(data, validate=True))
            except ValueError:
                valid = False
            if not valid:
                self.failed("登录二维码图片无效，请重试")
                return
            self.key = key
            self.qr.setPixmap(pixmap.scaled(280, 280, Qt.AspectRatioMode.KeepAspectRatio))
            self.status.setText("等待扫码；扫描后请在手机上确认登录")
            self.timer.start()

        self.job(self.client.create_qr, loaded, finished=lambda _: self.refresh.setEnabled(True))

    def poll(self):
        if not self.active or self.polling or not self.key:
            return
        self.polling = True
        key = self.key

        def checked(value):
            code = value["code"]
            if code == 800:
                self.timer.stop()
                self.status.setText("二维码已过期，请刷新二维码")
            elif code == 802:
                self.status.setText("已扫码，请在手机上确认登录")
            elif code == 803:
                self.timer.stop()
                self.refresh.setEnabled(False)
                self.status.setText("正在验证登录会话…")
                cookie = value.get("cookie", "")

                def logged_in(user):
                    self.host.netease_logged_in()
                    self.accept()

                self.job(
                    lambda: self.client.finish_login(cookie, cancelled=lambda: not self.active or self.host.closed),
                    logged_in,
                    finished=lambda _: self.refresh.setEnabled(True),
                )

        self.job(
            lambda: self.client.check_qr(key),
            checked,
            self.poll_failed,
            lambda _: setattr(self, "polling", False),
        )

    def poll_failed(self, message):
        self.timer.stop()
        self.failed(message)


class NeteaseSongDialog(ServiceDialog):
    def __init__(self, editor, picker=False):
        super().__init__(editor, editor.host)
        self.picker = picker
        self.setWindowTitle("选择网易云歌曲" if picker else "从网易云获取")
        self.resize(810, 530)
        self.selection = None
        self.songs = []
        self.offset = 0
        self.total = 0
        self.query = ""
        root = QVBoxLayout(self)
        root.addWidget(label("选择歌曲" if picker else "填写曲名、歌手与专辑封面", "heading"))
        self.session = label("", "muted")
        root.addWidget(self.session)
        row = QHBoxLayout()
        self.login_button = QPushButton("网易云登录")
        self.login_button.clicked.connect(self.login)
        row.addWidget(self.login_button)
        self.host.netease_auth_changed.connect(self.update_session)
        self.update_session()
        self.mode = QComboBox()
        self.mode.addItems(["歌曲链接 / ID", "歌曲名搜索"])
        self.mode.currentIndexChanged.connect(self.mode_changed)
        row.addWidget(self.mode)
        self.input = QLineEdit()
        self.input.setPlaceholderText("粘贴网易云歌曲分享链接或歌曲 ID")
        self.input.returnPressed.connect(self.lookup)
        row.addWidget(self.input, 1)
        self.fetch = QPushButton("获取")
        self.fetch.clicked.connect(self.lookup)
        row.addWidget(self.fetch)
        root.addLayout(row)
        self.status = label("链接 / ID 会定位指定歌曲；搜索后请选择正确版本。", "muted")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.table = QTableWidget(0, 6 if picker else 5)
        self.table.setHorizontalHeaderLabels(["序号", "封面", "歌曲名", "歌手", "专辑", "时长"] if picker
                                             else ["封面", "曲名", "歌手", "专辑", "歌曲 ID"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setIconSize(QSize(52, 52))
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 72)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemDoubleClicked.connect(lambda _: self.fill())
        root.addWidget(self.table, 1)
        controls = QHBoxLayout()
        self.previous = QPushButton("上一页")
        self.previous.clicked.connect(lambda: self.search_page(-10))
        self.next = QPushButton("下一页")
        self.next.clicked.connect(lambda: self.search_page(10))
        controls.addWidget(self.previous)
        controls.addWidget(self.next)
        controls.addStretch()
        self.text_only = QPushButton("仅填写文字，保留现有封面")
        self.text_only.clicked.connect(self.fill_text)
        self.text_only.hide()
        controls.addWidget(self.text_only)
        self.apply = QPushButton("选择此歌曲" if picker else "填写所选歌曲")
        self.apply.clicked.connect(self.fill)
        self.apply.setEnabled(False)
        controls.addWidget(self.apply)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        controls.addWidget(cancel)
        root.addLayout(controls)
        self.busy = False
        if picker:
            self.mode.setCurrentIndex(1)
            self.table.setColumnWidth(0, 48)
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(1, 68)
        self.mode_changed()

    def update_session(self):
        user = self.client.user
        name = str(user.get("nickname") or "已登录")
        text = f"已登录网易云：{name}" if user else "尚未登录网易云，可扫码登录"
        if self.host.netease_auth_restoring:
            text = "正在恢复网易云登录…"
        elif self.host.netease_restore_retry:
            text = "登录恢复失败，已保留保存的会话，可重试恢复"
        self.session.setText(text)
        self.login_button.setVisible(not user)
        self.login_button.setEnabled(not self.host.netease_auth_restoring)
        self.login_button.setText("重试恢复登录" if self.host.netease_restore_retry else "网易云登录")
        if hasattr(self, "busy"):
            self.set_busy(self.busy)

    def login(self):
        if not self.client.user:
            self.host.netease_login()
        self.update_session()

    def mode_changed(self):
        self.generation += 1
        self.input.setPlaceholderText(
            "输入歌曲名，可加上歌手名" if self.mode.currentIndex() else "粘贴网易云歌曲分享链接或歌曲 ID"
        )
        self.fetch.setText("搜索" if self.mode.currentIndex() else "获取并填写")
        self.songs = []
        self.table.setRowCount(0)
        self.offset = self.total = 0
        self.set_busy(False)

    def set_busy(self, busy):
        self.busy = busy
        self.fetch.setEnabled(not busy and not self.host.netease_auth_restoring)
        self.input.setEnabled(not busy)
        self.mode.setEnabled(not busy)
        self.table.setEnabled(not busy)
        self.apply.setEnabled(not busy and self.table.currentRow() >= 0)
        self.previous.setEnabled(not busy and self.offset > 0)
        self.next.setEnabled(not busy and self.offset + len(self.songs) < self.total)

    def selection_changed(self):
        self.text_only.hide()
        self.apply.setEnabled(not self.busy and self.table.currentRow() >= 0)

    def lookup(self):
        if self.busy:
            return
        if self.host.netease_auth_restoring:
            self.status.setText("正在恢复网易云登录，请稍候")
            return
        value = self.input.text().strip()
        if not value:
            self.status.setText("请输入歌曲链接、ID 或歌曲名")
            return
        self.generation += 1
        self.text_only.hide()
        self.songs = []
        self.table.setRowCount(0)
        self.offset = self.total = 0
        self.set_busy(True)
        self.status.setText("正在获取歌曲信息…")
        if self.mode.currentIndex():
            self.query = value
            self.job(lambda: self.client.search(value), self.show_results, self.lookup_failed)
        else:
            self.job(lambda: self.client.detail(value), self.direct_song, self.lookup_failed)

    def lookup_failed(self, message):
        self.failed(message)
        self.set_busy(False)

    def direct_song(self, song):
        self.show_results(([song], 1))
        self.table.selectRow(0)
        self.fill()

    def show_results(self, result):
        self.songs, self.total = result
        self.table.setRowCount(len(self.songs))
        for row, song in enumerate(self.songs):
            values = ([str(self.offset + row + 1), "加载中…", song.name, song.artists, song.album,
                       f"{int(song.duration_ms // 60000):02d}:{int(song.duration_ms / 1000) % 60:02d}"]
                      if self.picker else ["加载中…", song.name, song.artists, song.album, song.id])
            for col, text in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(text))
            self.table.setRowHeight(row, 62)
        self.status.setText(
            f"找到 {self.total} 首 · 请选择正确的歌手、专辑和版本"
            if self.songs
            else "没有搜索结果，请尝试其他歌曲名或加上歌手名"
        )
        self.set_busy(False)
        # Thumbnails run independently; selection remains usable while they download.
        for row, song in enumerate(self.songs):

            def loaded(data, row=row):
                pixmap = QPixmap()
                if pixmap.loadFromData(data):
                    self.table.item(row, 1 if self.picker else 0).setIcon(QIcon(pixmap))
                    self.table.item(row, 1 if self.picker else 0).setText("")
                else:
                    self.table.item(row, 1 if self.picker else 0).setText("封面无效")

            self.job(
                lambda song=song: self.client.cover(song),
                loaded,
                lambda _, row=row: self.table.item(row, 1 if self.picker else 0).setText("封面未加载"),
                priority=-1,
            )

    def search_page(self, delta):
        if self.busy:
            return
        self.generation += 1
        self.offset = max(0, self.offset + delta)
        self.text_only.hide()
        self.table.setRowCount(0)
        self.set_busy(True)
        self.status.setText("正在搜索…")
        offset, query = self.offset, self.query
        self.job(lambda: self.client.search(query, offset), self.show_results, self.lookup_failed)

    def fill(self):
        row = self.table.currentRow()
        if self.busy or not 0 <= row < len(self.songs):
            return
        song = self.songs[row]
        self.text_only.hide()
        self.set_busy(True)
        self.status.setText("正在获取所选歌曲的详情和封面…")

        def prepare():
            detail = self.client.detail(song.id)
            try:
                data = self.client.cover(detail)
                return detail, data, ""
            except NeteaseError as exc:
                return detail, None, str(exc)

        def prepared(value):
            detail, data, warning = value
            if data:
                pixmap = QPixmap()
                if not pixmap.loadFromData(data):
                    data, warning = None, "专辑封面图片无效，可重试或仅填写文字"
            self.set_busy(False)
            if warning:
                self.pending_song = detail
                self.status.setText(warning)
                self.text_only.show()
                return
            self.selection = detail, data
            self.accept()

        self.job(prepare, prepared, self.lookup_failed)

    def fill_text(self):
        self.selection = self.pending_song, None
        self.accept()
