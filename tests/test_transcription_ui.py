import threading
from types import SimpleNamespace

import numpy as np
from PySide6.QtWidgets import QMessageBox

from autoskyplay.netease import NeteaseAudio, NeteaseSong
from autoskyplay.score import Note, Score
from autoskyplay.storage import Store
from autoskyplay.transcription.melody import PitchResult
from autoskyplay.ui.main import MainWindow


def window_and_dialog(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("autoskyplay.ui.transcription.find_engine", lambda _: None)
    window = MainWindow(Store(tmp_path), no_audio=True, no_global=True, restore_netease=False)
    qtbot.addWidget(window)
    window.transcribe()
    dialog = window.transcription_dialog
    qtbot.addWidget(dialog)
    return window, dialog


def test_entry_reuses_window_and_shared_session(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    window.transcribe()
    assert window.transcription_dialog is dialog
    assert dialog.client is window.netease_client
    assert not dialog.begin.isEnabled()
    window.netease_client.user = {"nickname": "Listener"}
    window.netease_auth_changed.emit()
    assert "Listener" in dialog.account.text() and dialog.login.isHidden()
    dialog.close()
    assert not dialog.active
    window.close()


def test_trial_range_and_missing_engine_are_clear(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    monkeypatch.setattr(dialog.client, "detail", lambda _: NeteaseSong("1", "Example", "Artist", "Album", ""))
    monkeypatch.setattr(dialog.client, "playback_audio", lambda _: NeteaseAudio("1", "", 180000, 60, 90))
    dialog.identifier.setText("1")
    dialog.fetch_song()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert dialog.begin.isEnabled() and "试听" in dialog.song_info.text()
    assert dialog.start_time.value() == 60 and dialog.length.value() == 30
    dialog.start_task()
    assert dialog.backend_choice.currentData() == 'hybrid'
    assert 'GAME medium' in dialog.status.text() and dialog.task is None
    dialog.backend_choice.setCurrentIndex(dialog.backend_choice.findData('muscriptor'))
    dialog.start_task()
    assert "组件包" in dialog.status.text() and dialog.task is None
    dialog.close()
    window.close()


def test_remap_save_and_close_do_not_overwrite_existing_scores(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    dialog.song = NeteaseSong("1", "Example", "Artist", "Album", "")
    frequency = np.full(100, 440.)
    dialog.result = PitchResult(frequency, np.full(100, .9), np.full(100, -20.), np.zeros(100),
                                duration_ms=1000, metadata={"source_start": 60, "is_trial": True})
    dialog.remap()
    assert dialog.edit.isEnabled() and dialog.save.isEnabled()
    original = dialog.result.frequency.copy()
    dialog.auto_shift.setChecked(False)
    dialog.shift.setValue(5)
    np.testing.assert_equal(dialog.result.frequency, original)
    dialog.save_score()
    assert dialog.saved and len(list(window.store.songs_dir.glob("*.json"))) == 1
    dialog.save_score()
    assert len(list(window.store.songs_dir.glob("*.json"))) == 2
    qtbot.waitUntil(lambda: window.table.rowCount() == 2)
    dialog.minimum.setValue(90)
    dialog.flush_remap()
    assert not dialog.saved
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Cancel)
    assert window.close() and window.closed
    assert len(dialog.drafts.entries()) == 1
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Discard)
    assert window.close() and not dialog.active


def test_cancelled_lookup_ignores_late_result(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    gate = threading.Event()

    def detail(_):
        gate.wait(3)
        return NeteaseSong("1", "Late result", "", "", "")

    monkeypatch.setattr(dialog.client, "detail", detail)
    monkeypatch.setattr(dialog.client, "playback_audio", lambda _: NeteaseAudio("1", "", 30000))
    dialog.identifier.setText("1")
    dialog.fetch_song()
    dialog.cancel_task()
    gate.set()
    qtbot.waitUntil(lambda: not window.jobs)
    assert dialog.song is None and "Late result" not in dialog.song_info.text()
    dialog.close()
    window.close()


def test_component_cancel_waits_until_installation_finishes(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_: ("engine.zip", ""))
    gate = threading.Event()

    def install(path, directory, cancelled, progress):
        gate.wait(3)
        if cancelled():
            raise ValueError("组件安装已取消")
        return SimpleNamespace()

    monkeypatch.setattr("autoskyplay.ui.transcription.install_engine", install)
    dialog.install_component()
    dialog.cancel_task()
    assert dialog.busy and not dialog.begin.isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert not dialog.installing and "取消" in dialog.status.text()
    dialog.close()
    window.close()


def test_editor_saved_result_is_used_by_following_preview_and_save(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    dialog.score = Score("Draft", notes=[Note(0, 0)])
    edited = Score("Edited", bpm=90, notes=[Note(3, 0), Note(7, 400)])
    monkeypatch.setattr("autoskyplay.ui.transcription.EditorDialog",
                        lambda *a, **k: SimpleNamespace(exec=lambda: None, saved=True, score=edited))
    dialog.edit_score()
    assert dialog.score.name == "Edited" and dialog.bpm.value() == 90
    assert [n.key for n in dialog.score.notes] == [3, 7]
    dialog.save_score()
    from autoskyplay.score import load_score

    saved = load_score(next(window.store.songs_dir.glob("*.json")))
    assert [n.key for n in saved.notes] == [3, 7]
    dialog.close()
    window.close()
