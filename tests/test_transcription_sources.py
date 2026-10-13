import json
from types import SimpleNamespace

import mido
import numpy as np
import soundfile as sf
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox
from test_transcription_ui import window_and_dialog
from test_transcription_workbench import populate

from autoskyplay.netease import NeteaseAudio, NeteaseSong
from autoskyplay.transcription.cache import TTL, DownloadCache
from autoskyplay.transcription.local import MidiResult, read_midi
from autoskyplay.transcription.source import AudioSource
from autoskyplay.ui.dialogs import SettingsDialog
from autoskyplay.ui.netease import NeteaseSongDialog


def midi_file(path):
    midi = mido.MidiFile()
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.extend([mido.MetaMessage('set_tempo', tempo=500000),
                  mido.Message('note_on', note=60, velocity=80),
                  mido.Message('note_on', note=64, velocity=80),
                  mido.Message('note_off', note=60, time=480),
                  mido.Message('note_off', note=64),
                  mido.MetaMessage('set_tempo', tempo=1000000),
                  mido.Message('note_on', note=67, velocity=80),
                  mido.Message('note_off', note=67, time=480)])
    midi.save(path)
    return path


def test_cache_hit_ttl_not_extended_and_permanent_copy(tmp_path, monkeypatch):
    now = [1000.]
    monkeypatch.setattr('autoskyplay.transcription.cache.time.time', lambda: now[0])
    cache = DownloadCache(tmp_path)
    source = tmp_path / 'original'
    source.write_bytes(b'original-audio')
    audio = NeteaseAudio('1', 'https://secret-expiring-url', 30000)
    cache.put(audio, source)
    copy = tmp_path / 'owned-copy'
    now[0] += TTL - 1
    assert cache.get('1', copy).url == ''
    assert copy.read_bytes() == source.read_bytes()
    assert 'secret-expiring-url' not in next(cache.root.glob('*/entry.json')).read_text()
    now[0] += 1
    assert cache.get('1') is None
    assert copy.read_bytes() == source.read_bytes()
    assert not list(cache.root.iterdir())


def test_scope_mismatch_and_manual_clear_do_not_mix_sources(tmp_path):
    cache = DownloadCache(tmp_path)
    source = tmp_path / 'input'
    source.write_bytes(b'trial')
    trial = NeteaseAudio('1', '', 180000, 60, 90)
    full = NeteaseAudio('1', '', 180000)
    cache.put(trial, source)
    assert cache.get('1', audio=full) is None
    source.write_bytes(b'full')
    cache.put(full, source)
    assert cache.get('1', audio=trial).is_trial
    assert not cache.get('1', audio=full).is_trial
    assert cache.prune(clear=True) == 2
    assert source.read_bytes() == b'full'


def test_repeat_audio_preparation_downloads_once(tmp_path):
    calls = []
    def download(audio, path, cancelled):
        calls.append(audio.id)
        sf.write(path, np.ones(16000) * .3, 16000, format='WAV')
    song = NeteaseSong('1', '曲名', '', '', '', 1000)
    audio = NeteaseAudio('1', '', 1000)
    client = SimpleNamespace(download_audio=download)
    first = AudioSource(tmp_path, client, song, audio)
    first.prepare()
    first.close()
    second = AudioSource(tmp_path, client, song, audio)
    second.prepare()
    assert second.cached and calls == ['1']
    second.close()


def test_local_audio_never_uses_network_or_download_cache(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    path = tmp_path / '本地录音.wav'
    sf.write(path, np.ones(16000) * .3, 16000)
    def forbidden(*args, **kwargs):
        raise AssertionError('local source must stay offline')
    monkeypatch.setattr(dialog.client, 'playback_audio', forbidden)
    monkeypatch.setattr(dialog.client, 'download_audio', forbidden)
    dialog.import_local(path)
    qtbot.waitUntil(lambda: not dialog.busy)
    assert dialog.wave_data['kind'] == 'local_audio'
    assert dialog.wave_data['full_duration'] == 1
    assert dialog.source_dir.joinpath('source.audio').read_bytes() == path.read_bytes()
    assert not list(DownloadCache(tmp_path).root.glob('*/entry.json'))
    assert dialog.audio.available_seconds == 1
    window.close()
    assert path.exists()


def test_midi_import_preserves_polyphony_and_tempo_and_source(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    path = midi_file(tmp_path / '原始.mid')
    original = path.read_bytes()
    dialog.import_local(path)
    qtbot.waitUntil(lambda: not dialog.busy)
    assert isinstance(dialog.result, MidiResult)
    assert len(dialog.result.notes()) == 3
    assert [n.start_ms for n in dialog.result.notes()] == [0, 0, 500]
    assert [n.duration_ms for n in dialog.result.notes()] == [500, 500, 1000]
    ident = dialog.draft_id
    assert dialog.drafts.path(ident).joinpath('source.mid').read_bytes() == original
    assert dialog.task is None and dialog.preview_mode.currentIndex() == 2
    path.unlink()  # the user's original may move; the draft owns its copy
    dialog.close()
    window.transcribe()
    restored = window.transcription_dialog
    qtbot.addWidget(restored)
    restored.open_draft(restored.history_list.item(0))
    assert isinstance(restored.result, MidiResult) and restored.begin.isEnabled()
    assert not restored.restore_audio.isVisible()
    restored.set_selection(.5, 1.5)
    restored.start_task()
    assert restored.result_start() == .5 and restored.result.bpm == 60
    assert len(restored.result.notes()) == 1
    assert len(restored.drafts.entries()) == 2
    window.close()


def test_midi_crop_preserves_cross_boundary_note(tmp_path):
    path = midi_file(tmp_path / 'input.mid')
    result, duration = read_midi(path, start=.25, length=.5)
    assert duration == 1.5 and result.duration_ms == 500
    assert result.notes()[0].start_ms == 0 and result.notes()[0].duration_ms == 250
    assert result.notes()[-1].start_ms == 250 and result.notes()[-1].duration_ms == 250


def test_rename_survives_remap_and_does_not_rename_original_song(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    populate(dialog)
    ident = dialog.draft_id
    monkeypatch.setattr(QInputDialog, 'getText', lambda *a, **k: ('新的草稿名称', True))
    dialog.rename_draft()
    dialog.minimum.setValue(100)
    dialog.flush_remap()
    data, song, _, score = dialog.drafts.load(ident)
    assert data['title'] == score.name == '新的草稿名称'
    assert song.name == '草稿示例'
    assert not hasattr(dialog, 'delete_draft_button') and not hasattr(dialog, 'cache_button')
    window.close()


def test_settings_clears_downloads_but_not_draft_sources(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    populate(dialog)
    directory = dialog.drafts.path(dialog.draft_id)
    (directory / 'source.audio').write_bytes(b'source')
    (directory / 'vocals.wav').write_bytes(b'derivative')
    dialog.source_dir = dialog.media_dir = directory
    cache = DownloadCache(tmp_path)
    cache.put(NeteaseAudio('1', '', 3000), directory / 'source.audio')
    settings = SettingsDialog(window)
    qtbot.addWidget(settings)
    monkeypatch.setattr(QMessageBox, 'question', lambda *_: QMessageBox.StandardButton.Yes)
    settings.clear_audio_cache()
    assert (directory / 'source.audio').read_bytes() == b'source'
    assert not (directory / 'vocals.wav').exists()
    assert not list(cache.root.glob('*/entry.json'))
    assert dialog.score is not None
    settings.close()
    window.close()


def test_local_picker_returns_path(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    picker = NeteaseSongDialog(dialog, picker=True)
    qtbot.addWidget(picker)
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a, **k: (str(tmp_path / 'a.mid'), ''))
    picker.pick_local()
    assert picker.local_path.endswith('a.mid') and picker.selection is None
    window.close()


def test_app_start_expires_download_cache(tmp_path, qtbot, monkeypatch):
    cache = DownloadCache(tmp_path)
    audio = tmp_path / 'audio'
    audio.write_bytes(b'source')
    cache.put(NeteaseAudio('1', '', 1000), audio)
    entry = next(cache.root.glob('*/entry.json'))
    data = json.loads(entry.read_text())
    data['created'] -= TTL
    entry.write_text(json.dumps(data))
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    assert not list(cache.root.glob('*/entry.json'))
    window.close()


def test_restored_audio_draft_can_process_offline(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    populate(dialog)
    directory = dialog.drafts.path(dialog.draft_id)
    sf.write(directory / 'source.audio', np.zeros(48000), 16000, format='WAV')
    (directory / 'waveform.json').write_text(json.dumps(dict(kind='netease', peaks=[0], origin=60,
                                               duration=3, full_duration=180, trial=True)))
    dialog.close()
    window.transcribe()
    restored = window.transcription_dialog
    qtbot.addWidget(restored)
    restored.open_draft(restored.history_list.item(0))
    assert restored.audio.trial_start == 60 and restored.begin.isEnabled()
    assert not restored.restore_audio.isVisible()
    window.close()


def test_midi_selection_preserves_silent_tail(tmp_path):
    path = midi_file(tmp_path / 'tail.mid')
    midi = mido.MidiFile(path)
    midi.tracks[0][-1].time = 480  # one second of silence after the last note
    midi.save(path)
    result, duration = read_midi(path)
    assert duration == 2.5 and result.duration_ms == 2500
    assert max(n.start_ms + n.duration_ms for n in result.notes()) == 1500


def test_interrupted_draft_copy_does_not_modify_previous_source(tmp_path):
    import os

    from test_transcription_workbench import result

    from autoskyplay.score import Score
    from autoskyplay.transcription.drafts import DraftStore

    drafts = DraftStore(tmp_path)
    ident = 'a' * 32
    drafts.path(ident).mkdir()
    previous = tmp_path / 'previous.audio'
    previous.write_bytes(b'previous source')
    os.link(previous, drafts.path(ident) / 'source.audio.part')
    new_source = tmp_path / 'new'
    new_source.mkdir()
    (new_source / 'source.audio').write_bytes(b'new source')
    drafts.save(ident, NeteaseSong('1', 'Song', '', '', ''), result(), Score(), {}, source=new_source)
    assert previous.read_bytes() == b'previous source'
    assert (drafts.path(ident) / 'source.audio').read_bytes() == b'new source'
