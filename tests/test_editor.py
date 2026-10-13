from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QComboBox, QWidget

from autoskyplay.input import DEFAULT_KEYS
from autoskyplay.playback import Player, State
from autoskyplay.score import Note, Score, load_score
from autoskyplay.ui.editor import EditorDialog, NoteItem, encode_cover, pitch_name


class PreviewOutput:
    def __init__(self):
        self.keys = []

    def trigger(self, keys, durations=()):
        self.keys.extend(keys)

    def pump(self):
        pass

    def silence(self):
        pass

    def finish(self):
        pass


@pytest.fixture
def editor(qtbot, tmp_path):
    host = QWidget()
    host.store = SimpleNamespace(data={"keys": list(DEFAULT_KEYS)}, songs_dir=tmp_path)
    host.instrument = QComboBox(host)
    for name in ("Piano", "Cello"):
        host.instrument.addItem(name, name)
    host.pitch_value = 0
    host.instrument.currentIndexChanged.connect(
        lambda index: setattr(host, "pitch_value", -12 if index else 0),
    )
    host.player = Player()
    host.mixer = PreviewOutput()
    host.ensure_audio = lambda: True
    host.errors = []
    host.show_error = host.errors.append
    score = Score("原曲", author="原作者", transcriber="做谱者", bpm=120,
                  notes=[Note(0, 0), Note(7, 3500)], root={"custom": "保留"})
    score.save(tmp_path / "score.json")
    dialog = EditorDialog(host, score)
    qtbot.addWidget(dialog, before_close_func=lambda widget: setattr(widget, "dirty", False))
    qtbot.addWidget(host)
    dialog.show()
    return dialog


def cover_image():
    image = QImage(900, 650, QImage.Format.Format_RGB32)
    image.fill(QColor("#cc7755"))
    return image


@pytest.mark.parametrize("dismiss", ["cancel", "close"])
def test_edit_info_entry_and_cancel_restore_staged_metadata_and_cover(editor, qtbot, dismiss):
    original = editor.score.to_data()
    opened = []

    def cancel_edits():
        info = editor.info_dialog
        opened.append(info.isVisible())
        info.name.setText("修改曲名")
        info.author.setText("修改作者")
        info.embed_cover(cover_image())
        if dismiss == "cancel":
            qtbot.mouseClick(info.cancel, Qt.MouseButton.LeftButton)
        else:
            info.close()

    assert not editor.name.isVisible()
    QTimer.singleShot(0, cancel_edits)
    qtbot.mouseClick(editor.info_button, Qt.MouseButton.LeftButton)
    assert opened == [True]
    assert editor.score.to_data() == original
    assert editor.name.text() == "原曲"
    assert editor.info_dialog.cover is None
    assert not editor.dirty


def test_metadata_confirmation_and_cover_removal_are_saved(editor, qtbot):
    info = editor.info_dialog
    info.show()
    info.name.setText(" 新曲名 ")
    info.author.setText("新作者")
    info.transcriber.setText("新做谱者")
    info.embed_cover(cover_image())
    qtbot.mouseClick(info.confirm, Qt.MouseButton.LeftButton)
    assert editor.dirty
    assert editor.score.name == editor.song_summary.text() == "新曲名"
    assert editor.save()
    saved = load_score(editor.score.path)
    assert (saved.name, saved.author, saved.transcriber) == ("新曲名", "新作者", "新做谱者")
    assert saved.root["custom"] == "保留"
    assert [(n.key, n.time_ms, n.duration_ms) for n in saved.notes] == [
        (n.key, n.time_ms, n.duration_ms) for n in editor.score.notes
    ]
    assert saved.root["cover"] == encode_cover(cover_image())

    info.reload()
    info.show()
    qtbot.mouseClick(info.remove_cover, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(info.cancel, Qt.MouseButton.LeftButton)
    assert editor.score.root["cover"] == saved.root["cover"] and not editor.dirty
    info.show()
    qtbot.mouseClick(info.remove_cover, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(info.confirm, Qt.MouseButton.LeftButton)
    assert editor.dirty and "cover" not in editor.score.root
    assert editor.save() and "cover" not in load_score(editor.score.path).root


def test_netease_metadata_is_staged_until_confirmation(editor, qtbot):
    info = editor.info_dialog
    info.show()
    song = SimpleNamespace(name="网易云曲名", artists="歌手", album="专辑", id=123)
    assert info.apply_netease(song, None)
    assert info.name.text() == "网易云曲名" and info.author.text() == "歌手"
    assert editor.score.name == "原曲" and not editor.dirty
    qtbot.mouseClick(info.cancel, Qt.MouseButton.LeftButton)
    assert editor.score.name == info.name.text() == "原曲"


def test_progress_and_scrolled_ruler_seek_without_editing_notes(editor, qtbot):
    original = editor.score.to_data()
    qtbot.mouseClick(editor.progress, Qt.MouseButton.LeftButton,
                     pos=QPoint(editor.progress.width() // 2, editor.progress.height() // 2))
    assert abs(editor.roll.cursor_beat - editor.roll.total_beats / 2) < 0.1
    assert editor.roll.horizontalScrollBar().value() > 0
    point = QPoint(180, 14)
    x = editor.roll.mapToScene(point.x(), 0).x()
    expected = round(x / editor.roll.pixels_per_beat * editor.roll.subdivision) / editor.roll.subdivision
    qtbot.mouseClick(editor.ruler, Qt.MouseButton.LeftButton, pos=point)
    assert editor.roll.cursor_beat == expected
    assert editor.progress.value() == round(expected * 1000)
    assert editor.score.to_data() == original and not editor.dirty


def test_preview_seeks_pauses_and_moves_through_gaps_between_notes(editor, qtbot):
    editor.progress.setValue(2000)
    editor.preview()
    assert editor.player.state == State.PLAYING
    qtbot.waitUntil(lambda: editor.roll.cursor_beat > 2.1, timeout=1500)
    assert not editor.host.mixer.keys  # The first note is before the cursor; the next is at beat 7.
    editor.preview()
    assert editor.player.state == State.PAUSED
    editor.progress.setValue(6000)
    assert editor.player.position_ms == 3000
    editor.preview()
    qtbot.waitUntil(lambda: editor.roll.cursor_beat > 6.1, timeout=1500)
    editor.stop_preview(rewind=True)
    assert editor.player.state == State.STOPPED
    assert editor.roll.cursor_beat == editor.progress.value() == 0


def test_high_to_low_rows_write_delete_and_undo_the_correct_keys(editor, qtbot):
    assert pitch_name(0) == "C4" and pitch_name(7) == "C5" and pitch_name(14) == "C6"
    assert editor.roll.key_y(14) < editor.roll.key_y(0)
    for key, beat in ((14, 3), (0, 4)):
        point = editor.roll.mapFromScene(QPointF(
            beat * editor.roll.pixels_per_beat, editor.roll.key_y(key) + editor.roll.row_height / 2,
        ))
        qtbot.mouseClick(editor.roll.viewport(), Qt.MouseButton.LeftButton, pos=point)
        assert any(note.key == key and note.time_ms == beat * 500 for note in editor.score.notes)
    high_note = next(note for note in editor.score.notes if note.key == 14)
    point = editor.roll.mapFromScene(QPointF(3 * editor.roll.pixels_per_beat + 3,
                                            editor.roll.key_y(14) + 5))
    qtbot.mouseClick(editor.roll.viewport(), Qt.MouseButton.RightButton, pos=point)
    assert high_note not in editor.score.notes
    editor.undo()
    assert high_note in editor.score.notes
    assert editor.save()
    assert any(note.key == 14 and note.time_ms == 1500 for note in load_score(editor.score.path).notes)


def test_keyboard_cursor_and_audition_keys(editor, qtbot):
    editor.roll.setFocus()
    qtbot.keyClick(editor.roll, Qt.Key.Key_Right)
    assert editor.roll.cursor_beat == 0.25 and editor.progress.value() == 250
    qtbot.keyClick(editor.roll, Qt.Key.Key_Y)
    assert any(note.key == 0 and note.time_ms == 125 for note in editor.score.notes)
    assert editor.host.mixer.keys == [0]
    original = editor.score.to_data()
    qtbot.mouseClick(editor.key_buttons[14], Qt.MouseButton.LeftButton)
    assert editor.host.mixer.keys[-1] == 14 and editor.score.to_data() == original
    editor.instrument.setCurrentIndex(1)
    assert editor.pitch_axis.offset == -12
    assert "C3" in editor.key_buttons[0].toolTip()
    assert any(isinstance(item, NoteItem) for item in editor.roll.scene().items())


def test_dragging_a_note_changes_pitch_and_time_and_can_be_undone(editor, qtbot):
    roll = editor.roll
    note = editor.score.notes[1]
    start = roll.mapFromScene(QPointF(7 * roll.pixels_per_beat + 4, roll.key_y(7) + 7))
    end = start + QPoint(roll.pixels_per_beat, -2 * roll.row_height)
    qtbot.mousePress(roll.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(roll.viewport(), pos=end)
    qtbot.mouseRelease(roll.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert note.key == 9 and note.time_ms == 4000
    editor.undo()
    assert editor.score.notes[1].key == 7 and editor.score.notes[1].time_ms == 3500
