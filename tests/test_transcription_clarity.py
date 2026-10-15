"""Independent musical invariants for the piano/violin performance profiles."""
from copy import deepcopy

import numpy as np
import pytest
from test_transcription_quality import layered_result
from test_transcription_ui import window_and_dialog

from autoskyplay.midi import SKY_MIDI
from autoskyplay.netease import NeteaseSong
from autoskyplay.score import Note, Score
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.melody import MelodyNote, to_score
from autoskyplay.transcription.performance import generation_mode, piano_tails, sparse_accompaniment
from autoskyplay.transcription.register import melody_keys


def identity(score):
    return [(n.key, n.time_ms, n.duration_ms, n.extra['sourcePitch'])
            for n in score.notes if n.extra['role'] == 'melody']


def test_instrument_defaults_and_explicit_accompaniment_keep_same_lead():
    result = layered_result()
    original = result.frequency.copy()
    piano, _ = build_score(result, 'Piano')
    violin, _ = build_score(result, 'Violin', instrument='Violin')
    optional, _ = build_score(result, 'Violin+', instrument='Violin', mode='simple')
    assert identity(piano) == identity(violin) == identity(optional)
    assert any(n.extra['role'] == 'accompaniment' for n in piano.notes)
    assert all(n.extra['role'] == 'melody' for n in violin.notes)
    assert any(n.extra['role'] == 'accompaniment' for n in optional.notes)
    sections = piano.root['transcription']['sections']
    assert sections == violin.root['transcription']['sections'] == optional.root['transcription']['sections']
    for score in (piano, violin, optional):
        for n in score.notes:
            assert not any(n.time_ms < s['end_ms'] and n.time_ms + n.duration_ms > s['start_ms']
                           for s in sections if s['needs_review'])
    np.testing.assert_equal(result.frequency, original)


def support_score(lead, support):
    notes = [Note(key, start, duration, dict(role='melody')) for key, start, duration in lead]
    notes += [Note(0, start, 300, dict(role='accompaniment', sourcePitch=pitch, sourceTime=start))
              for start, pitch in support]
    return Score(bpm=120, notes=notes, root={'transcription': dict(shift=0, chords=[])})


def test_accompaniment_waits_for_real_piano_decay_and_spaces_repeated_support():
    score = support_score([(9, 0, 6000)], [(0, 48), (1000, 55), (1600, 48), (2500, 48), (4000, 55)])
    lead = deepcopy(score.notes[0])
    sparse_accompaniment(score, 'Piano')
    accepted = [n for n in score.notes if n.extra.get('role') == 'accompaniment']
    assert score.notes[1] == lead  # the melody itself is never shortened or moved
    assert [n.time_ms for n in accepted] == [0, 2500]
    for a, b in zip(accepted, accepted[1:]):
        assert b.time_ms >= a.time_ms + piano_tails()[a.key]
    assert len({n.time_ms for n in accepted}) == len(accepted)


def test_quiet_piano_decay_does_not_discard_later_melody_passing_tone():
    # E5 over C4 is clear. D5 arrives after C4's stronger envelope, while
    # its quiet tail is still present; it must not remove the earlier support.
    score = support_score([(9, 0, 800), (8, 1000, 500)], [(0, 48)])
    sparse_accompaniment(score, 'Piano')
    assert any(n.extra['role'] == 'accompaniment' for n in score.notes)
    clash = support_score([(8, 0, 800)], [(0, 48)])
    sparse_accompaniment(clash, 'Piano')
    assert all(n.extra['role'] == 'melody' for n in clash.notes)


def test_busy_melody_and_low_register_take_priority_over_support():
    busy = support_score([(9, t, 100) for t in (0, 150, 300, 450, 600)], [(0, 48)])
    sparse_accompaniment(busy, 'Piano')
    assert len(busy.notes) == 5
    assert busy.root['transcription']['clarity']['rejected'] == {'busy_melody': 1}
    low = support_score([(0, 0, 800)], [(0, 48)])
    sparse_accompaniment(low, 'Piano')
    assert len(low.notes) == 1


@pytest.mark.parametrize('pitches', [[78, 79, 81, 83, 84, 86], [86, 84, 83, 81, 79, 78]])
def test_phrase_crossing_keyboard_edge_preserves_each_step(pitches):
    notes = [MelodyNote(p, i * 300, 250, .9) for i, p in enumerate(pitches)]
    keys = melody_keys(notes, 0)
    mapped = [SKY_MIDI[keys[i]] for i in range(len(notes))]
    assert all((b - a) * (d - c) > 0 for a, b, c, d in zip(pitches, pitches[1:], mapped, mapped[1:]))
    assert max(abs(b - a) for a, b in zip(mapped, mapped[1:])) <= 3


def test_repeated_motif_and_rearticulation_keep_register_without_erasing_real_leaps():
    pitches = [60, 72, 72, 67, 64]
    first = [MelodyNote(p, i * 300, 250, .9) for i, p in enumerate(pitches)]
    second = [MelodyNote(p, 3000 + i * 700, 600, .9) for i, p in enumerate(pitches)]
    keys = melody_keys(first + second, 0)
    assert [keys[i] for i in range(5)] == [keys[i] for i in range(5, 10)]
    assert keys[1] == keys[2]
    assert SKY_MIDI[keys[1]] - SKY_MIDI[keys[0]] == 12
    score, _ = to_score(first, 'Motif', shift=0, rhythm='original')
    assert [(n.time_ms, n.duration_ms, n.extra['sourcePitch']) for n in score.notes] == [
        (n.start_ms, n.duration_ms, n.pitch) for n in first]


def test_unplayable_chromatic_long_note_is_retained_and_marked():
    score, _ = to_score([MelodyNote(61, 100, 900, .9)], 'Chromatic', shift=0)
    assert len(score.notes) == 1 and score.notes[0].duration_ms == 900
    assert score.notes[0].extra['mappingReview']
    assert score.root['transcription']['mapping_review'][0]['start_ms'] == 100


def test_invalid_profile_fails_before_generation():
    with pytest.raises(ValueError):
        generation_mode('auto', 'Missing instrument')
    with pytest.raises(ValueError):
        generation_mode('unknown', 'Piano')


def test_ui_profile_switch_and_edited_draft_are_durable(qtbot, tmp_path, monkeypatch):
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    dialog.song = NeteaseSong('1', 'Profile reference', '', '', '')
    dialog.result = layered_result()
    dialog.remap()
    lead = identity(dialog.score)
    dialog.instrument.setCurrentIndex(dialog.instrument.findData('Violin'))
    dialog.flush_remap()
    assert identity(dialog.score) == lead
    assert dialog.score.root['transcription']['mode'] == 'melody'
    assert all(n.extra['role'] == 'melody' for n in dialog.score.notes)
    ident = dialog.draft_id
    dialog.close()
    window.transcribe()
    dialog = window.transcription_dialog
    qtbot.addWidget(dialog)
    dialog.open_draft(dialog.history_list.item(0))
    assert dialog.draft_id == ident and dialog.generation_mode.currentData() == 'auto'
    assert dialog.instrument.currentData() == 'Violin'
    dialog.generation_mode.setCurrentIndex(dialog.generation_mode.findData('simple'))
    dialog.flush_remap()
    assert any(n.extra['role'] == 'accompaniment' for n in dialog.score.notes)
    dialog.edited = True
    dialog.score.notes[0].key = 3
    dialog.persist_draft()
    edited = dialog.score.to_data()
    edited_id = dialog.draft_id
    dialog.instrument.setCurrentIndex(dialog.instrument.findData('Piano'))
    dialog.flush_remap()
    assert dialog.score.to_data() == edited and dialog.draft_id == edited_id
    dialog.generation_mode.setCurrentIndex(dialog.generation_mode.findData('auto'))
    dialog.flush_remap()
    assert dialog.draft_id != edited_id
    assert dialog.drafts.load(edited_id)[3].to_data() == edited
    dialog.set_busy(True)
    assert not dialog.instrument.isEnabled()
    dialog.set_busy(False)
    window.close()
