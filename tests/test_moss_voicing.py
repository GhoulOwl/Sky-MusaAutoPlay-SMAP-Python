from copy import deepcopy

import pytest

from autoskyplay.midi import SKY_MIDI
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.events import EventResult, ModelNote
from autoskyplay.transcription.moss import parse_lines
from autoskyplay.transcription.rich_harmony import strong_tails


def fixture(chords, *, lead_pitch=64, shift=0):
    analysis = {'energy_db': [-20]*40, 'moss_music': parse_lines(chords, '', 4000)}
    result = EventResult([ModelNote(lead_pitch, 0, 1800, 'voice', 0),
                          ModelNote(lead_pitch, 2500, 600, 'voice', 1)], 4000, 120, {}, analysis)
    direct, _ = build_score(result, 'direct', mode='moss_direct', shift=shift, rhythm='original')
    optimized, _ = build_score(result, 'optimized', mode='moss_optimized', shift=shift, rhythm='original')
    return result, direct, optimized


def notes(score, role):
    return [n for n in score.notes if n.extra.get('role') == role]


def test_voicings_preserve_melody_and_avoid_same_key_and_strong_tails():
    result, direct, optimized = fixture('0.2s: Cmaj7\n2s: Am7\n')
    original = deepcopy(result.analysis)
    assert [(n.key, n.time_ms, n.duration_ms) for n in notes(direct, 'melody')] == [
        (n.key, n.time_ms, n.duration_ms) for n in notes(optimized, 'melody')]
    tails = strong_tails('Piano')
    for accompaniment in notes(optimized, 'accompaniment'):
        for melody in notes(optimized, 'melody'):
            if accompaniment.key == melody.key:
                assert (accompaniment.time_ms + max(accompaniment.duration_ms, tails[accompaniment.key])
                        <= melody.time_ms or
                        melody.time_ms + max(melody.duration_ms, tails[melody.key]) <= accompaniment.time_ms)
        assert not accompaniment.extra['approximated']
        assert SKY_MIDI[accompaniment.key] % 12 == accompaniment.extra['sourcePitch'] % 12
    assert notes(optimized, 'accompaniment')
    assert result.analysis == original


def test_black_key_chords_skip_unplayable_tones_or_fall_back_without_silence():
    _, _, mixed = fixture('0.2s: C#maj7\n')
    support = notes(mixed, 'accompaniment')
    assert support and all(not n.extra['approximated'] for n in support)
    assert mixed.root['transcription']['moss_optimized']['omitted_chromatic'] > 0
    assert any(n.extra.get('mossDoubled') for n in support)
    _, _, black_only = fixture('0.2s: F#\n')
    assert notes(black_only, 'accompaniment')
    assert all(n.extra['approximated'] for n in notes(black_only, 'accompaniment'))
    assert black_only.root['transcription']['moss_optimized']['decisions'][0]['fallback_all_chromatic']
    _, _, transposed = fixture('0.2s: F#\n', shift=1)
    assert notes(transposed, 'accompaniment')
    assert all(not n.extra['approximated'] for n in notes(transposed, 'accompaniment'))


def test_direct_mode_remains_available_and_optimized_honors_manual_rest():
    result, direct, _ = fixture('0.2s: Cmaj7\n')
    optimized, _ = build_score(result, 'rest', mode='moss_optimized', shift=0, rhythm='original',
                               overrides=[{'start_ms': 1000, 'end_ms': 2000, 'source': 'rest'}])
    assert direct.root['transcription']['mode'] == 'moss_direct'
    for note in notes(optimized, 'accompaniment'):
        assert note.time_ms + note.duration_ms <= 1000 or note.time_ms >= 2000


def test_optimized_mode_keeps_four_supported_voices_when_possible():
    result = EventResult([ModelNote(74, 0, 500, 'voice', 0)], 4000, 120, {}, {
        'energy_db': [-20]*40, 'moss_music': parse_lines('1s: Cmaj7\n', '', 4000)})
    score, _ = build_score(result, 'four', mode='moss_optimized', shift=0, rhythm='original')
    assert len([n for n in notes(score, 'accompaniment') if n.time_ms == 1000]) == 4


def test_four_voice_limit_reports_omitted_chord_tones():
    result = EventResult([ModelNote(74, 0, 500, 'voice', 0)], 4000, 120, {}, {
        'energy_db': [-20]*40, 'moss_music': parse_lines('1s: Cmaj9\n', '', 4000)})
    score, _ = build_score(result, 'five', mode='moss_optimized', shift=0, rhythm='original')
    info = score.root['transcription']['moss_optimized']
    assert info['omitted_voice_limit'] == 1
    assert info['decisions'][0]['omitted_for_voice_limit'] == [2]


def test_optimized_mode_gui_and_draft_round_trip(qtbot, tmp_path, monkeypatch):
    from test_transcription_ui import window_and_dialog

    from autoskyplay.netease import NeteaseSong

    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    dialog.song = NeteaseSong('1', 'Optimized', '', '', '')
    dialog.result = fixture('0.2s: C#maj7\n')[0]
    dialog.generation_mode.setCurrentIndex(dialog.generation_mode.findData('moss_optimized'))
    dialog.flush_remap()
    assert dialog.score.root['transcription']['mode'] == 'moss_optimized'
    assert '和弦半音省略' in dialog.statistics.text()
    assert dialog.drafts.load(dialog.draft_id)[3].root['transcription']['moss_optimized']['policy'] == 'moss-voicing-v1'
    dialog.close()
    window.close()


def test_switching_an_edited_draft_to_optimized_forks_the_draft(qtbot, tmp_path, monkeypatch):
    from test_transcription_ui import window_and_dialog

    from autoskyplay.netease import NeteaseSong

    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    dialog.song = NeteaseSong('1', 'Edited', '', '', '')
    dialog.result = fixture('0.2s: Cmaj7\n')[0]
    dialog.generation_mode.setCurrentIndex(dialog.generation_mode.findData('moss_direct'))
    dialog.flush_remap()
    previous = dialog.draft_id
    dialog.edited = True
    assert dialog.persist_draft()
    dialog.generation_mode.setCurrentIndex(dialog.generation_mode.findData('moss_optimized'))
    dialog.flush_remap()
    assert dialog.draft_id != previous
    assert dialog.drafts.load(previous)[3].root['transcription']['mode'] == 'moss_direct'
    assert dialog.drafts.load(dialog.draft_id)[3].root['transcription']['mode'] == 'moss_optimized'
    dialog.close()
    window.close()


def test_moss_modes_do_not_allow_refinement_to_drop_model_accompaniment():
    from autoskyplay.transcription.refinement import Region, make_candidate

    result, _, optimized = fixture('0.2s: Cmaj7\n')
    region = Region.create(result, 0, 2000)
    with pytest.raises(ValueError, match='MOSS 和弦模式'):
        make_candidate('a'*32, result, optimized, {'mode': 'moss_optimized'}, result, region)
