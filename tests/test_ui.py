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
    store = Store(tmp_path)
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
