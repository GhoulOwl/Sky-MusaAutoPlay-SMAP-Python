from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QEnterEvent
from PySide6.QtWidgets import QApplication

from autoskyplay.playback import State
from autoskyplay.score import Note, Score, load_score
from autoskyplay.storage import Store
from autoskyplay.ui.dialogs import SettingsDialog
from autoskyplay.ui.editor import EditorDialog, NoteItem
from autoskyplay.ui.main import MainWindow
from autoskyplay.ui.practice import PracticeDialog


def make_window(qtbot, tmp_path):
    import socket

    store = Store(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        store.data["netease_port"] = sock.getsockname()[1]
    score = store.add_score(Score("Example", notes=[Note(0, 0), Note(4, 1000)]))
    window = MainWindow(store, no_audio=True, no_global=True)
    qtbot.addWidget(window)
    window.show()
    return window, score


def test_library_queue_and_restore(qtbot, tmp_path):
    window, score = make_window(qtbot, tmp_path)
    assert window.table.rowCount() == 1
    window.add_queue(score)
    window.select_score(score)
    window.player.seek(600)
    window.save_state()
    assert Store(tmp_path).data["position_ms"] == 600
    assert Store(tmp_path).data["queue"] == [str(score.path)]
    window.close()


def test_editor_undo_redo_saves_notes(qtbot, tmp_path):
    window, score = make_window(qtbot, tmp_path)
    editor = EditorDialog(window, score)
    qtbot.addWidget(editor)
    editor.roll.add_note(7, 4)
    assert len(editor.score.notes) == 3
    editor.undo()
    assert len(editor.score.notes) == 2
    editor.redo()
    assert len(editor.score.notes) == 3
    assert editor.save()
    assert len(load_score(score.path).notes) == 3
    assert len(score.notes) == 2
    editor.close()
    window.close()


def test_imported_off_grid_note_does_not_move_on_editor_open(qtbot, tmp_path):
    window, score = make_window(qtbot, tmp_path)
    score.notes[1].time_ms = 987.65
    editor = EditorDialog(window, score)
    qtbot.addWidget(editor)
    item = next(i for i in editor.roll.scene().items() if isinstance(i, NoteItem) and i.note.key == 4)
    assert abs(item.x() - 987.65 / 500 * 70) < 0.001
    assert editor.save()
    assert load_score(score.path).notes[1].time_ms == 987.65
    editor.close()
    window.close()


def test_practice_dialog_closes_and_releases_held_notes(qtbot, tmp_path):
    window, score = make_window(qtbot, tmp_path)
    dialog = PracticeDialog(window, score)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.press(0)
    assert dialog.practice.index == 1
    dialog.close()
    assert not window.mixer.voices
    window.close()


def test_background_job_returns_on_ui_thread(qtbot, tmp_path):
    import threading

    window, score = make_window(qtbot, tmp_path)
    threads = []
    window.run_job(
        lambda: threading.get_ident(), lambda result: threads.append((result, threading.get_ident()))
    )
    qtbot.waitUntil(lambda: bool(threads))
    assert threads[0][0] != threading.get_ident()
    assert threads[0][1] == threading.get_ident()
    window.close()


def test_playback_order_cycles_and_restores(qtbot, tmp_path):
    window, _ = make_window(qtbot, tmp_path)
    for expected in ("list", "shuffle", "single", "sequence"):
        qtbot.mouseClick(window.loop_button, Qt.MouseButton.LeftButton)
        assert window.playback_order == expected
    window.save_state()
    window.close()
    restored = MainWindow(Store(tmp_path), no_audio=True, no_global=True)
    qtbot.addWidget(restored)
    assert restored.playback_order == "sequence"
    restored.close()


@pytest.mark.parametrize("order", ["sequence", "list", "shuffle", "single"])
def test_queue_completion_obeys_playback_order(qtbot, tmp_path, order):
    window, first = make_window(qtbot, tmp_path)
    last = window.store.add_score(Score("Last", notes=[Note(0, 0)]))
    window.add_queue(first)
    window.add_queue(last)
    window.playback_order = order
    window.select_score(last)
    window.player.state = State.FINISHED
    window.finished_song()
    assert window.autoplay.isActive() == (order != "sequence")
    window.next_song(play=False, automatic=True)
    assert window.current.path == (last.path if order in ("sequence", "single") else first.path)
    window.close()


def test_single_repeat_allows_manual_next_after_completion(qtbot, tmp_path):
    window, first = make_window(qtbot, tmp_path)
    last = window.store.add_score(Score("Last", notes=[Note(0, 0)]))
    window.add_queue(first)
    window.add_queue(last)
    window.playback_order = "single"
    window.select_score(first)
    window.player.state = State.FINISHED
    window.next_song(play=False)
    assert window.current.path == last.path
    window.close()


def test_queue_visibility_preserves_playback_and_instrument_panel(qtbot, tmp_path):
    window, score = make_window(qtbot, tmp_path)
    window.add_queue(score, play=True)
    assert window.queue_panel.isHidden()
    assert not window.right.isAncestorOf(window.queue_list)
    qtbot.mouseClick(window.queue_button, Qt.MouseButton.LeftButton)
    assert window.queue_panel.isVisible()
    assert window.player.state == State.PLAYING
    assert window.right.isVisible()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    qtbot.keyClick(window.queue_button, Qt.Key.Key_Escape)
    assert window.queue_panel.isHidden()
    assert not window.queue_button.isChecked()
    assert window.player.state == State.PLAYING
    window.queue_button.click()
    window.clear_queue()
    assert window.queue_empty.isVisible()
    assert not window.clear_queue_button.isEnabled()
    window.queue_button.click()
    assert window.queue_panel.isHidden()
    window.close()


def test_output_mode_buttons_are_exclusive_and_stop_playback(qtbot, tmp_path):
    window, score = make_window(qtbot, tmp_path)
    window.add_queue(score, play=True)
    assert window.preview_button.isChecked()
    # Exercise the Windows-only choice without sending any game input.
    window.perform_button.setEnabled(True)
    qtbot.mouseClick(window.perform_button, Qt.MouseButton.LeftButton)
    assert window.perform_button.isChecked()
    assert not window.preview_button.isChecked()
    assert window.player.state == State.STOPPED
    qtbot.mouseClick(window.preview_button, Qt.MouseButton.LeftButton)
    assert window.preview_button.isChecked()
    assert not window.perform_button.isChecked()
    window.close()


def test_hover_volume_remains_usable_and_saves(qtbot, tmp_path):
    window, score = make_window(qtbot, tmp_path)
    window.add_queue(score, play=True)
    QApplication.sendEvent(window.volume_button, QEnterEvent(QPointF(), QPointF(), QPointF()))
    qtbot.waitUntil(window.volume_button.popup.isVisible)
    QApplication.sendEvent(window.volume_button, QEvent(QEvent.Type.Leave))
    QApplication.sendEvent(window.volume_button.popup, QEnterEvent(QPointF(), QPointF(), QPointF()))
    qtbot.wait(200)
    assert window.volume_button.popup.isVisible()
    window.volume.setValue(35)
    assert window.mixer.volume == 0.35
    assert window.player.state == State.PLAYING
    # A drag outside the popup must finish before it can hide.
    window.volume.setSliderDown(True)
    QApplication.sendEvent(window.volume_button.popup, QEvent(QEvent.Type.Leave))
    qtbot.wait(200)
    assert window.volume_button.popup.isVisible()
    window.volume.setSliderDown(False)
    qtbot.waitUntil(window.volume_button.popup.isHidden)
    window.volume.setValue(0)
    assert "0%" in window.volume_button.accessibleName()
    window.save_state()
    assert Store(tmp_path).data["volume"] == 0
    window.close()


def test_pitch_selection_updates_audio_and_restores_each_instrument(qtbot, tmp_path, monkeypatch):
    window, _ = make_window(qtbot, tmp_path)
    prepared = []
    window.no_audio = False
    window.audio_output = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(window.mixer, "prepare", lambda *values: prepared.append(values))
    window.pitch.setCurrentIndex(window.pitch.findData(24))
    assert window.ensure_audio()
    assert prepared[-1] == ("Piano", 24)
    window.instrument.setCurrentIndex(window.instrument.findData("Contrabass"))
    assert window.pitch_value == -24
    assert window.ensure_audio()
    assert prepared[-1] == ("Contrabass", -24)
    window.instrument.setCurrentIndex(window.instrument.findData("Piano"))
    assert window.pitch.currentData() == 24
    window.save_state()
    window.close()
    restored = MainWindow(Store(tmp_path), no_audio=True, no_global=True)
    qtbot.addWidget(restored)
    assert restored.pitch.currentData() == 24
    restored.close()


def test_random_speed_setting_survives_player_rebuild(qtbot, tmp_path, monkeypatch):
    window, _ = make_window(qtbot, tmp_path)

    def accept_settings(dialog):
        dialog.random_speed.setChecked(True)
        dialog.save()
        return dialog.DialogCode.Accepted

    monkeypatch.setattr(SettingsDialog, "exec", accept_settings)
    window.settings()
    assert window.player.random_speed
    window.save_state()
    assert Store(tmp_path).data["random_speed"]
    window.close()


def netease_song(identifier="186016", name="晴天", album="叶惠美"):
    from autoskyplay.netease import NeteaseSong
    return NeteaseSong(identifier, name, "周杰伦", album, "https://p1.music.126.net/cover.jpg")


def cover_bytes():
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage
    image = QImage(900, 650, QImage.Format.Format_RGB32)
    image.fill(QColor("#cc7755"))
    array = QByteArray()
    buffer = QBuffer(array)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(array)


def test_netease_editor_fills_metadata_and_compressed_cover_preserving_score(qtbot, tmp_path):
    import base64

    from PySide6.QtGui import QImage

    window, score = make_window(qtbot, tmp_path)
    score.transcriber = "创谱者"
    score.root["custom"] = {"preserved": True}
    editor = EditorDialog(window, score)
    qtbot.addWidget(editor)
    editor.apply_netease(netease_song(), cover_bytes())
    assert editor.dirty and editor.name.text() == "晴天"
    assert editor.author.text() == "周杰伦" and editor.transcriber.text() == "创谱者"
    assert editor.score.notes == score.notes
    assert score.name == "Example" and "cover" not in score.root
    compressed = base64.b64decode(editor.score.root["cover"])
    image = QImage.fromData(compressed)
    assert compressed.startswith(b"\xff\xd8") and max(image.width(), image.height()) == 512
    assert editor.save()
    saved = load_score(score.path)
    assert saved.name == "晴天" and saved.author == "周杰伦" and saved.transcriber == "创谱者"
    assert saved.root["custom"] == {"preserved": True} and saved.root["cover"] == editor.score.root["cover"]
    # A text-only retry keeps the existing cover and remains manually editable.
    editor.apply_netease(netease_song(name="另一个版本"), None)
    assert editor.score.root["cover"] == saved.root["cover"]
    editor.name.setText("手动改名")
    assert editor.save() and load_score(score.path).name == "手动改名"
    editor.close()
    window.close()


def test_netease_search_requires_version_selection(qtbot, tmp_path, monkeypatch):
    from autoskyplay.ui.netease import NeteaseSongDialog

    window, score = make_window(qtbot, tmp_path)
    original, live = netease_song(), netease_song("999", "晴天 (Live)", "演唱会")
    lookups = []
    window.netease_client.user = {"nickname": "Listener"}
    monkeypatch.setattr(window.netease_client, "search", lambda name, offset=0: ([original, live], 2))
    monkeypatch.setattr(window.netease_client, "cover", lambda song: cover_bytes())
    monkeypatch.setattr(window.netease_client, "detail", lambda identifier: lookups.append(identifier) or live)
    editor = EditorDialog(window, score)
    qtbot.addWidget(editor)
    dialog = NeteaseSongDialog(editor)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.mode.setCurrentIndex(1)
    dialog.input.setText("晴天")
    dialog.lookup()
    assert dialog.busy
    qtbot.waitUntil(lambda: dialog.table.rowCount() == 2 and not dialog.busy)
    assert not dialog.selection and not dialog.apply.isEnabled()
    assert editor.name.text() == "Example" and "Listener" in dialog.session.text()
    assert dialog.table.item(1, 1).text() == "晴天 (Live)"
    assert dialog.table.item(1, 3).text() == "演唱会"
    dialog.table.selectRow(1)
    dialog.fill()
    qtbot.waitUntil(lambda: dialog.selection is not None)
    assert lookups == ["999"] and dialog.selection[0] == live
    editor.apply_netease(*dialog.selection)
    assert editor.name.text() == "晴天 (Live)"
    assert editor.save()
    editor.close()
    window.close()


def test_netease_empty_results_failure_and_manual_fallback(qtbot, tmp_path, monkeypatch):
    from autoskyplay.netease import NeteaseError
    from autoskyplay.ui.netease import NeteaseSongDialog

    window, score = make_window(qtbot, tmp_path)
    monkeypatch.setattr(window.netease_client, "search", lambda name: ([], 0))
    editor = EditorDialog(window, score)
    qtbot.addWidget(editor)
    dialog = NeteaseSongDialog(editor)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.mode.setCurrentIndex(1)
    dialog.input.setText("不存在")
    dialog.lookup()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert "没有搜索结果" in dialog.status.text() and not dialog.apply.isEnabled()
    dialog.mode.setCurrentIndex(0)
    monkeypatch.setattr(window.netease_client, "detail", lambda value: netease_song())

    def no_cover(song):
        raise NeteaseError("专辑封面下载失败")

    monkeypatch.setattr(window.netease_client, "cover", no_cover)
    dialog.input.setText("186016")
    dialog.lookup()
    qtbot.waitUntil(dialog.text_only.isVisible)
    assert "封面下载失败" in dialog.status.text()
    assert editor.name.text() == "Example"
    dialog.fill_text()
    assert dialog.selection == (netease_song(), None)
    editor.close()
    window.close()


def test_netease_closed_search_ignores_late_results(qtbot, tmp_path, monkeypatch):
    import threading

    from autoskyplay.ui.netease import NeteaseSongDialog

    window, score = make_window(qtbot, tmp_path)
    started, release = threading.Event(), threading.Event()

    def search(name):
        started.set()
        release.wait(timeout=3)
        return [netease_song()], 1

    monkeypatch.setattr(window.netease_client, "search", search)
    editor = EditorDialog(window, score)
    qtbot.addWidget(editor)
    dialog = NeteaseSongDialog(editor)
    qtbot.addWidget(dialog)
    dialog.mode.setCurrentIndex(1)
    dialog.input.setText("晴天")
    dialog.lookup()
    qtbot.waitUntil(started.is_set)
    dialog.reject()
    release.set()
    qtbot.waitUntil(lambda: not window.jobs)
    assert dialog.table.rowCount() == 0 and not dialog.selection
    assert editor.name.text() == "Example"
    editor.close()
    window.close()


def test_netease_qr_login_updates_home_and_stops_polling(qtbot, tmp_path, monkeypatch):
    import base64

    from autoskyplay.ui.netease import NeteaseLoginDialog, NeteaseSongDialog

    window, _ = make_window(qtbot, tmp_path)
    monkeypatch.setattr(window.netease_client, "create_qr",
                        lambda: ("qr-key", base64.b64encode(cover_bytes()).decode()))
    statuses = iter([{"code": 802}, {"code": 803, "cookie": "MUSIC_U=secret"}])
    monkeypatch.setattr(window.netease_client, "check_qr", lambda key: next(statuses))

    def finish(cookie, cancelled):
        assert cookie == "MUSIC_U=secret" and not cancelled()
        window.netease_client.cookie = cookie
        window.netease_client.remembered = True
        window.netease_client.user = {"userId": 1, "nickname": "Listener"}
        return window.netease_client.user

    monkeypatch.setattr(window.netease_client, "finish_login", finish)
    dialog = NeteaseLoginDialog(window)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: bool(dialog.key))
    dialog.timer.stop()
    dialog.poll()
    qtbot.waitUntil(lambda: not dialog.polling)
    assert "手机上确认" in dialog.status.text()
    dialog.poll()
    qtbot.waitUntil(lambda: not dialog.active)
    assert "Listener" in window.netease_account.text() and not dialog.timer.isActive()
    assert Store(tmp_path).data["remember_netease_login"]
    assert "MUSIC_U" not in window.store.path.read_text()
    editor = EditorDialog(window, Score("Example"))
    qtbot.addWidget(editor)
    lookup = NeteaseSongDialog(editor)
    qtbot.addWidget(lookup)
    lookup.show()
    assert lookup.client is window.netease_client
    assert lookup.login_button.isHidden()
    assert "Listener" in lookup.session.text()

    def detail(identifier):
        assert window.netease_client.cookie == "MUSIC_U=secret"
        return netease_song(identifier)

    # Even a stale/programmatic login action must reuse the home session.
    monkeypatch.setattr(window, "netease_login", lambda: pytest.fail("主页已登录时不应再次登录"))
    lookup.login()
    monkeypatch.setattr(window.netease_client, "detail", detail)
    monkeypatch.setattr(window.netease_client, "cover", lambda song: cover_bytes())
    lookup.input.setText("186016")
    lookup.lookup()
    qtbot.waitUntil(lambda: lookup.selection is not None)
    assert lookup.selection[0].name == "晴天"
    editor.close()
    window.close()


def test_netease_settings_reject_occupied_port_without_changing_data(qtbot, tmp_path, monkeypatch):
    import socket

    from PySide6.QtWidgets import QMessageBox

    window, _ = make_window(qtbot, tmp_path)
    dialog = SettingsDialog(window)
    qtbot.addWidget(dialog)
    warnings = []
    old_port = window.store.data["netease_port"]
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dialog.netease_port.setValue(sock.getsockname()[1])
        dialog.save()
    assert warnings and "被占用" in warnings[0]
    assert window.store.data["netease_port"] == old_port
    dialog.reject()
    window.close()


def test_netease_startup_restores_saved_session_and_editor_tracks_progress(qtbot, tmp_path, monkeypatch,
                                                                         credential_store):
    import threading

    from autoskyplay.auth import save_netease_auth
    from autoskyplay.netease import NeteaseClient
    from autoskyplay.ui.netease import NeteaseSongDialog

    store = Store(tmp_path)
    store.data["remember_netease_login"] = True
    store.save()
    save_netease_auth(tmp_path, "MUSIC_U=saved")
    started, release = threading.Event(), threading.Event()

    def request(client, route, **params):
        assert route == "/login/status" and client.cookie == "MUSIC_U=saved"
        started.set()
        release.wait(timeout=3)
        return {"data": {"profile": {"userId": 1, "nickname": "Listener"}}}

    monkeypatch.setattr(NeteaseClient, "request", request)
    window = MainWindow(Store(tmp_path), no_audio=True, no_global=True)
    qtbot.addWidget(window)
    qtbot.waitUntil(started.is_set)
    assert window.netease_auth_restoring and not window.netease_account.isEnabled()
    editor = EditorDialog(window, Score("Example"))
    qtbot.addWidget(editor)
    lookup = NeteaseSongDialog(editor)
    qtbot.addWidget(lookup)
    lookup.show()
    assert "正在恢复" in lookup.session.text() and not lookup.fetch.isEnabled()
    release.set()
    qtbot.waitUntil(lambda: not window.netease_auth_restoring)
    assert "Listener" in window.netease_account.text()
    assert "Listener" in lookup.session.text() and lookup.login_button.isHidden()
    assert lookup.fetch.isEnabled() and window.netease_client.cookie == "MUSIC_U=saved"
    assert Store(tmp_path).data["remember_netease_login"]
    assert "MUSIC_U" not in window.store.path.read_text()
    lookup.reject()
    editor.close()
    window.close()


def test_netease_logout_disables_restore_even_when_keyring_delete_fails(qtbot, tmp_path, monkeypatch,
                                                                      credential_store):
    from PySide6.QtWidgets import QMessageBox

    from autoskyplay.auth import load_netease_auth, save_netease_auth
    from autoskyplay.netease import NeteaseError

    window, _ = make_window(qtbot, tmp_path)
    save_netease_auth(tmp_path, "MUSIC_U=saved")
    window.netease_client.cookie = "MUSIC_U=saved"
    window.netease_client.user = {"userId": 1, "nickname": "Listener"}
    window.netease_client.remembered = True
    window.store.data["remember_netease_login"] = True
    errors = []
    monkeypatch.setattr(window, "show_error", errors.append)

    def delete(*args):
        raise RuntimeError("credential service unavailable")

    def request(*args, **kwargs):
        raise NeteaseError("网络不可用")

    monkeypatch.setattr("autoskyplay.auth.keyring.delete_password", delete)
    monkeypatch.setattr(window.netease_client, "request", request)
    monkeypatch.setattr(QMessageBox, "exec", lambda dialog: QMessageBox.StandardButton.Yes)
    window.logout_netease()
    assert not Store(tmp_path).data["remember_netease_login"]
    qtbot.waitUntil(lambda: not window.jobs)
    assert not window.netease_client.user and not window.netease_client.cookie
    assert errors and "无法清除" in errors[0]
    assert load_netease_auth(tmp_path) == "MUSIC_U=saved"
    window.close()
    restarted = MainWindow(Store(tmp_path), no_audio=True, no_global=True)
    qtbot.addWidget(restarted)
    assert not restarted.netease_auth_restoring and not restarted.netease_client.user
    restarted.close()


@pytest.mark.parametrize("service", ["cloud", "netease"])
def test_logged_in_account_requires_double_click_and_logout_confirmation(qtbot, tmp_path, monkeypatch,
                                                                        credential_store, service):
    from PySide6.QtCore import QAbstractAnimation
    from PySide6.QtWidgets import QMessageBox

    from autoskyplay.auth import load_auth, load_netease_auth, save_auth, save_netease_auth
    from autoskyplay.ui.dialogs import LoginDialog
    from autoskyplay.ui.netease import NeteaseLoginDialog

    window, _ = make_window(qtbot, tmp_path)
    if service == "cloud":
        user = {"username": "Listener", "mid": "saved-token"}
        window.cloud_client.user = user.copy()
        save_auth(window.cloud_client.base_url, user)
        button = window.account
        flag = "remember_login"
        name = "缪斯树屋"
    else:
        window.netease_client.user = {"userId": 1, "nickname": "Listener"}
        window.netease_client.cookie = "MUSIC_U=saved"
        window.netease_client.remembered = True
        save_netease_auth(tmp_path, "MUSIC_U=saved")
        monkeypatch.setattr(window.netease_client, "request", lambda *args, **kwargs: {"code": 200})
        button = window.netease_account
        flag = "remember_netease_login"
        name = "网易云"

    def saved():
        return load_auth(window.cloud_client.base_url) if service == "cloud" else load_netease_auth(tmp_path)

    def session():
        return window.cloud_client.user if service == "cloud" else window.netease_client.user
    window.store.data[flag] = True
    window.save_state()
    original_session, original_saved = session().copy(), saved()
    confirmations = []
    answer = QMessageBox.StandardButton.Cancel

    def warning(dialog):
        confirmations.append(dialog)
        return answer

    monkeypatch.setattr(QMessageBox, "exec", warning)
    # The second release after a successful logout must not open sign-in again.
    monkeypatch.setattr(LoginDialog, "exec", lambda dialog: pytest.fail("Unexpected sign-in dialog"))
    monkeypatch.setattr(NeteaseLoginDialog, "exec", lambda dialog: pytest.fail("Unexpected QR dialog"))
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert button.animation.state() == QAbstractAnimation.State.Running
    assert session() == original_session and saved() == original_saved
    assert not confirmations and window.store.data[flag]
    qtbot.keyClick(button, Qt.Key.Key_Space)
    qtbot.mouseDClick(button, Qt.MouseButton.RightButton)
    assert not confirmations and session() == original_session

    qtbot.mouseDClick(button, Qt.MouseButton.LeftButton)
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton)
    assert len(confirmations) == 1 and name in confirmations[0].text()
    assert confirmations[0].icon() == QMessageBox.Icon.Warning
    assert confirmations[0].defaultButton() == confirmations[0].button(QMessageBox.StandardButton.Cancel)
    assert session() == original_session and saved() == original_saved
    assert Store(tmp_path).data[flag]

    answer = QMessageBox.StandardButton.Yes
    qtbot.mouseDClick(button, Qt.MouseButton.LeftButton)
    qtbot.mouseRelease(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not window.jobs)
    assert len(confirmations) == 2 and not session() and not saved()
    window.save_state()
    assert not Store(tmp_path).data[flag]
    if service == "netease":
        assert not window.netease_client.cookie
    window.close()


@pytest.mark.parametrize("service", ["cloud", "netease"])
def test_logged_out_account_still_signs_in_on_single_click(qtbot, tmp_path, monkeypatch, service):
    from autoskyplay.ui.dialogs import LoginDialog
    from autoskyplay.ui.netease import NeteaseLoginDialog

    window, _ = make_window(qtbot, tmp_path)
    dialogs = []

    def sign_in(dialog):
        dialogs.append(dialog)
        dialog.reject()
        return dialog.DialogCode.Rejected

    dialog_type = LoginDialog if service == "cloud" else NeteaseLoginDialog
    monkeypatch.setattr(dialog_type, "exec", sign_in)
    qtbot.mouseClick(window.account if service == "cloud" else window.netease_account,
                     Qt.MouseButton.LeftButton)
    assert len(dialogs) == 1
    window.close()
