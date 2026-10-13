from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QMessageBox
from test_transcription_ui import window_and_dialog

from autoskyplay.netease import NeteaseAudio, NeteaseSong
from autoskyplay.score import Note, Score
from autoskyplay.transcription.drafts import DraftStore
from autoskyplay.transcription.melody import MelodyNote, PitchResult, to_score
from autoskyplay.transcription.source import AudioSource
from autoskyplay.ui.editor import EditorDialog
from autoskyplay.ui.transcription_widgets import PreviewRoll, Waveform


def result():
    return PitchResult(np.full(300, 440.), np.full(300, .9), np.full(300, -20.), np.zeros(300),
                       duration_ms=3000, bpm=100, metadata={'source_start': 60, 'requested_length': 3})


def populate(dialog):
    dialog.song = NeteaseSong('1', '草稿示例', '歌手', '专辑', '', 180000)
    dialog.result = result()
    dialog.set_selection(60, 63)
    dialog.remap()


def test_draft_roundtrip_and_audio_pruning_preserves_edits(tmp_path):
    drafts = DraftStore(tmp_path)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'source.audio').write_bytes(b'audio')
    (source / 'original.wav').write_bytes(b'original')
    song = NeteaseSong('1', '歌名', '歌手', '专辑', '', 180000)
    score = Score('Edited', notes=[Note(3, 100)])
    ident = drafts.save(None, song, result(), score, {}, edited=True, source=source, media=source)
    drafts.prune_audio(0)
    data, loaded_song, loaded_result, loaded_score = drafts.load(ident)
    assert loaded_song == song and data['edited']
    assert loaded_score.notes[0].key == 3
    assert loaded_result.metadata['source_start'] == 60
    assert (drafts.path(ident) / 'pitch.npz').exists()
    assert (drafts.path(ident) / 'source.audio').exists()
    assert not (drafts.path(ident) / 'original.wav').exists()
    assert (source / 'source.audio').exists()  # hard-linked cache eviction doesn't touch its origin
    with pytest.raises(ValueError):
        drafts.path('../outside')


def test_draft_lru_and_corrupt_entry(tmp_path):
    drafts = DraftStore(tmp_path)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'original.wav').write_bytes(b'0123456789')
    ids = [drafts.save(None, NeteaseSong('1', str(i), '', '', ''), result(), Score(), {}, media=source)
           for i in range(2)]
    drafts.load(ids[0])
    drafts.prune_audio(10)
    assert (drafts.path(ids[0]) / 'original.wav').exists()
    assert not (drafts.path(ids[1]) / 'original.wav').exists()
    (drafts.path(ids[1]) / 'draft.json').write_text('broken')
    assert len(drafts.entries()) == 1


def test_audio_preparation_real_waveform_and_cancel(tmp_path):
    def download(audio, path, cancelled):
        sf.write(path, np.sin(np.arange(16000) / 16), 16000, format='WAV')
    song = NeteaseSong('1', 'A', '', '', '', 180000)
    source = AudioSource(tmp_path, SimpleNamespace(download_audio=download), song,
                         NeteaseAudio('1', '', 180000, 60, 61))
    wave = source.prepare()
    assert wave['origin'] == 60 and wave['duration'] == 1
    assert len(wave['peaks']) == 50 and max(wave['peaks']) > .9
    assert (source.work / 'source.audio').exists()
    source.close()
    assert not source.work.exists()
    cancelled = AudioSource(tmp_path, SimpleNamespace(download_audio=download), song,
                            NeteaseAudio('1', '', 1000))
    cancelled.close()
    with pytest.raises(ValueError, match='取消'):
        cancelled.prepare()
    assert not cancelled.work.exists()


def test_grid_offset_and_default_preserves_timing():
    notes = [MelodyNote(60, 180, 200, .9)]
    original, _ = to_score(notes, 'A', bpm=120)
    aligned, _ = to_score(notes, 'A', bpm=120, quantize=4, grid_offset_ms=50)
    assert original.notes[0].time_ms == 180
    assert aligned.notes[0].time_ms == 175
    assert aligned.root['transcription']['grid_offset_ms'] == 50


def test_waveform_handles_clamp_trial_and_move(qtbot):
    wave = Waveform()
    qtbot.addWidget(wave)
    wave.resize(1000, 108)
    wave.set_data(dict(peaks=[.5] * 50, origin=60, duration=30, full_duration=180))
    wave.set_range(65, 75)
    wave.show()
    start = QPoint(round(wave.x(65)), 60)
    qtbot.mousePress(wave, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(wave, QPoint(round(wave.x(20)), 60))
    qtbot.mouseRelease(wave, Qt.MouseButton.LeftButton, pos=QPoint(round(wave.x(20)), 60))
    assert wave.start == 60 and wave.end == 75
    qtbot.mousePress(wave, Qt.MouseButton.LeftButton, pos=QPoint(round(wave.x(67)), 60))
    qtbot.mouseMove(wave, QPoint(round(wave.x(170)), 60))
    qtbot.mouseRelease(wave, Qt.MouseButton.LeftButton, pos=QPoint(round(wave.x(170)), 60))
    assert wave.end == 90 and wave.start == 75


def test_preview_roll_never_mutates_score(qtbot):
    roll = PreviewRoll()
    qtbot.addWidget(roll)
    score = Score(notes=[Note(0, 0), Note(2, 1000)])
    roll.set_score(score, 3000)
    original = score.to_data()
    roll.show()
    qtbot.mouseClick(roll.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(80, 70))
    qtbot.mouseClick(roll.viewport(), Qt.MouseButton.RightButton, pos=QPoint(5, 5))
    roll.delete_selected()
    roll.add_note(5)
    assert score.to_data() == original


def test_edit_then_remap_forks_without_losing_manual_notes(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    populate(dialog)
    ident = dialog.draft_id
    dialog.score = Score('手工修改', notes=[Note(2, 0), Note(4, 1000)])
    dialog.edited = True
    assert dialog.persist_draft()
    dialog.minimum.setValue(100)
    dialog.flush_remap()
    assert dialog.draft_id != ident
    data, _, _, edited = dialog.drafts.load(ident)
    assert data['edited'] and [n.key for n in edited.notes] == [2, 4]
    assert len(dialog.drafts.entries()) == 2
    window.close()


def test_restore_draft_after_close_without_audio_or_inference(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    populate(dialog)
    ident = dialog.draft_id
    dialog.close()
    window.transcribe()
    restored = window.transcription_dialog
    qtbot.addWidget(restored)
    restored.open_draft(restored.history_list.item(0))
    assert restored.draft_id == ident
    assert restored.song.name == '草稿示例' and restored.result_start() == 60
    assert restored.score and restored.preview_mode.currentIndex() == 2
    assert restored.grid.currentData() == 0 and restored.display_grid.currentData() == 4
    assert not restored.begin.isEnabled()
    assert restored.restore_audio.isVisible()
    window.close()


def test_changed_range_keeps_old_result_and_its_origin(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    populate(dialog)
    notes = dialog.score.to_data()
    dialog.set_selection(90, 120)
    assert dialog.result_start() == 60 and dialog.score.to_data() == notes
    assert '上一范围' in dialog.status.text()
    window.close()


def test_failed_autosave_blocks_closing(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    populate(dialog)
    def fail(*args, **kwargs):
        raise OSError('磁盘已满')
    monkeypatch.setattr(dialog.drafts, 'save', fail)
    monkeypatch.setattr(QMessageBox, 'question', lambda *_: QMessageBox.StandardButton.Cancel)
    assert not dialog.close() and dialog.active
    monkeypatch.setattr(QMessageBox, 'question', lambda *_: QMessageBox.StandardButton.Discard)
    window.close()


def test_editor_draft_mode_does_not_write_library(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    editor = EditorDialog(window, Score('Draft', notes=[Note(2, 0)]), draft_mode=True)
    qtbot.addWidget(editor)
    editor.name.setText('Edited')
    assert editor.save()
    assert editor.saved and editor.score.name == 'Edited' and editor.score.path is None
    assert not list(window.store.songs_dir.glob('*.json'))
    window.close()


def test_picker_duration_and_columns(qtbot, tmp_path, monkeypatch):
    from autoskyplay.ui.netease import NeteaseSongDialog
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    picker = NeteaseSongDialog(dialog, picker=True)
    qtbot.addWidget(picker)
    monkeypatch.setattr(picker, 'job', lambda *args, **kwargs: None)
    song = NeteaseSong.parse(dict(id=1, name='A', dt=183000))
    assert asdict(song)['duration_ms'] == 183000
    picker.show_results(([song], 1))
    assert picker.table.columnCount() == 6
    assert picker.table.item(0, 5).text() == '03:03'
    assert picker.table.item(0, 0).text() == '1'
    picker.close()
    window.close()


def test_preview_source_and_result_positions_are_separate(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    populate(dialog)
    dialog.set_selection(61, 63)
    dialog.wave_seek(62)
    assert dialog.audition_source and dialog.seek.value() == 1000
    dialog.preview_mode.setCurrentIndex(2)
    assert not dialog.audition_source and dialog.seek.value() == 2000
    dialog.roll_seek(750)
    assert dialog.seek.value() == 750
    window.close()


def test_audio_restore_does_not_change_draft_when_scope_changed(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    populate(dialog)
    ident = dialog.draft_id
    monkeypatch.setattr(dialog.client, 'playback_audio', lambda _: NeteaseAudio('1', '', 180000, 0, 30))
    dialog.recover_audio()
    qtbot.waitUntil(lambda: not dialog.busy)
    assert dialog.draft_id == ident and dialog.result_start() == 60
    assert '不包含' in dialog.status.text()
    window.close()


def test_clear_audio_keeps_edit_and_raw_data(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    populate(dialog)
    ident = dialog.draft_id
    directory = dialog.drafts.path(ident)
    (directory / 'source.audio').write_bytes(b'cached')
    dialog.source_dir = directory
    dialog.score.notes = [Note(8, 100)]
    dialog.edited = True
    monkeypatch.setattr(QMessageBox, 'question', lambda *_: QMessageBox.StandardButton.Yes)
    dialog.clear_audio_cache()
    assert (directory / 'source.audio').exists()
    data, _, _, score = dialog.drafts.load(ident)
    assert data['edited'] and score.notes[0].key == 8
    assert (directory / 'pitch.npz').exists()
    window.close()


def test_task_reuses_prepared_source_without_downloading(tmp_path, monkeypatch):
    from autoskyplay.transcription.task import TranscriptionTask
    source = tmp_path / 'cached.audio'
    source.write_bytes(b'prepared source')
    def forbidden(*args, **kwargs):
        raise AssertionError('prepared audio must not be downloaded again')
    task = TranscriptionTask(tmp_path, SimpleNamespace(download_audio=forbidden, cover=lambda _: None),
                             NeteaseSong('1', 'A', '', '', ''), NeteaseAudio('1', '', 3000),
                             None, 0, 3, source=source)
    def run():
        assert (task.work / 'source.audio').read_bytes() == b'prepared source'
        raw = result()
        np.savez_compressed(task.work / 'pitch.npz', **{key: getattr(raw, key) for key in
                            ('frequency', 'confidence', 'energy_db', 'onset', 'hop_ms', 'duration_ms', 'bpm')})
        return {}
    monkeypatch.setattr(task, '_run_engine', run)
    task.start()
    task.thread.join(5)
    assert task.result is not None and task.result.metadata['source_start'] == 0
    task.close()
    assert source.exists() and not task.work.exists()


def test_cached_draft_uses_retained_source_after_remapping(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    populate(dialog)
    directory = dialog.drafts.path(dialog.draft_id)
    (directory / 'source.audio').write_bytes(b'cached')
    dialog.close()
    window.transcribe()
    restored = window.transcription_dialog
    qtbot.addWidget(restored)
    restored.open_draft(restored.history_list.item(0))
    restored.minimum.setValue(100)
    restored.flush_remap()
    assert not restored.restore_audio.isVisible() and restored.begin.isEnabled()
    window.close()
