"""Musical invariants and independent references for the A–D quality pipeline."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import mido
import numpy as np
import pytest

from autoskyplay.netease import NeteaseSong
from autoskyplay.transcription.arrangement import arrange, build_score, chord_from_chroma
from autoskyplay.transcription.drafts import DraftStore
from autoskyplay.transcription.melody import MelodyNote, export_midi
from autoskyplay.transcription.rhythm import snap_interval

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('benchmark', ROOT / 'scripts/evaluate_transcription.py')
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)
CASES = json.loads((ROOT / 'tests/fixtures/transcription/contours.json').read_text())['cases']


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['name'])
def test_independent_segmentation_references(case):
    result = benchmark.fixture(case)
    raw = result.frequency.copy()
    predicted = [[n.pitch, n.start_ms, n.duration_ms] for n in result.notes()]
    assert benchmark.metrics(case['notes'], predicted)['f1'] == 1
    np.testing.assert_equal(result.frequency, raw)


def layered_result():
    # Intro 0–2 s; singer 2–5 s with 0.4 s breath; solo 5–8 s;
    # audible ambiguity 8–10 s; real silence 10–12 s.
    count = 1200
    def track(notes):
        return benchmark.fixture(dict(duration=12000, notes=notes))
    vocal = track([[72, 2000, 1000], [74, 3400, 1600]])
    other = track([[67, 0, 900], [69, 1000, 900], [71, 5000, 900],
                   [72, 6000, 900], [74, 7000, 900]])
    bass = track([[48, 0, 10000]])
    for name, result in (('other', other), ('bass', bass)):
        vocal.analysis.update({name + '_' + key: getattr(result, key)
                               for key in ('frequency', 'confidence', 'energy_db', 'onset')})
    vocal.analysis.update(vocal_ratio=np.where(vocal.energy_db > -65, .8, .01),
                          other_ratio=np.full(count, .7),
                          mix_energy_db=np.r_[np.full(1000, -15), np.full(200, -100)],
                          beats_ms=np.arange(0, 12000, 500.), beat_confidence=np.array(.9),
                          chroma=np.tile([1, 0, 0, 0, .9, 0, 0, .9, 0, 0, 0, 0], (23, 1)),
                          harmony_energy_db=np.full(23, -20.))
    return vocal


def test_intro_solo_and_uncertainty_never_filled():
    result = layered_result()
    notes, info = arrange(result, mode='melody')
    assert any(n.source == 'other' and n.start_ms == 0 for n in notes)
    assert any(n.source == 'vocals' and n.start_ms == 2000 for n in notes)
    assert any(n.source == 'other' and n.start_ms == 6000 for n in notes)
    assert not any(3000 <= n.start_ms < 3400 for n in notes)  # breath
    assert not any(n.start_ms >= 8000 for n in notes)
    assert any(s['needs_review'] and s['start_ms'] <= 8000 and s['end_ms'] >= 10000 for s in info['sections'])


def test_sparse_accompaniment_has_evidence_and_never_hides_melody():
    result = layered_result()
    melody, _ = build_score(result, 'Reference', mode='melody', shift=0, rhythm='original')
    full, stats = build_score(result, 'Reference', shift=0, rhythm='original')
    lead = [n for n in full.notes if n.extra['role'] == 'melody']
    assert [(n.key, n.time_ms) for n in lead] == [(n.key, n.time_ms) for n in melody.notes]
    assert stats['accompaniment'] > 0
    for time in sorted({n.time_ms for n in full.notes}):
        active = [n for n in full.notes if n.time_ms <= time < n.time_ms + n.duration_ms]
        accompaniment = [n for n in active if n.extra['role'] == 'accompaniment']
        assert len(accompaniment) <= 1
        assert len({n.key for n in active}) == len(active)
    assert not any(n.time_ms >= 8000 for n in full.notes)
    generated = [n for n in full.notes if n.extra.get('generated')]
    assert generated and all(n.extra['source'] == 'harmony' for n in generated)


def test_ambiguous_chords_and_unreliable_beats_are_not_guessed():
    assert chord_from_chroma(np.ones(12)) is None
    assert chord_from_chroma([1] + [0] * 11) is None  # a bass pitch is not a chord
    result = layered_result()
    result.analysis['beat_confidence'] = np.array(.1)
    notes, info = arrange(result)
    assert all(n.role == 'melody' for n in notes)
    assert info['warnings']


def test_override_rest_and_vocals_cannot_fabricate_notes():
    result = layered_result()
    notes, _ = arrange(result, overrides=[dict(start_ms=0, end_ms=2000, source='rest')])
    assert not any(n.start_ms < 2000 for n in notes)
    notes, info = arrange(result, overrides=[dict(start_ms=8000, end_ms=10000, source='vocals')])
    assert not any(8000 <= n.start_ms < 10000 for n in notes)
    assert any(s['needs_review'] for s in info['sections'])
    with pytest.raises(ValueError):
        arrange(result, overrides=[dict(start_ms=-1, end_ms=20, source='other')])


def test_rhythm_preserves_untracked_time_and_joint_endpoints():
    beats = [150, 660, 1180, 1710]
    assert snap_interval(50, 120, 120, 4, beats=beats) == (50, 120)
    assert snap_interval(290, 655, 120, 4, beats=beats, mode='gentle', confidence=.9) == (277.5, 660)
    assert snap_interval(290, 655, 120, 4, beats=beats, mode='gentle', confidence=.1) == (290, 655)
    assert snap_interval(166, 334, 120, 3) == pytest.approx((500 / 3, 1000 / 3))
    assert snap_interval(251, 255, 120, 4) == (251, 255)  # collapsed short note stays intact


def test_layered_analysis_and_annotations_survive_drafts_and_cache_clear(tmp_path):
    result = layered_result()
    score, _ = build_score(result, 'Round trip')
    drafts = DraftStore(tmp_path)
    source = tmp_path / 'source'
    source.mkdir()
    for name in ('source.audio', 'other.wav', 'bass.wav', 'drums.wav'):
        (source / name).write_bytes(b'test')
    ident = drafts.save(None, NeteaseSong('1', 'A', '', '', ''), result, score,
                        dict(mode='simple'), media=source, source=source)
    drafts.prune_audio(0)
    _, _, restored, saved = drafts.load(ident)
    np.testing.assert_equal(restored.analysis['chroma'], result.analysis['chroma'])
    assert saved.root['transcription']['sections'] == score.root['transcription']['sections']
    assert (drafts.path(ident) / 'source.audio').exists()
    assert not (drafts.path(ident) / 'other.wav').exists()
    again, _ = build_score(restored, 'Round trip')
    assert again.to_data() == score.to_data()


def test_raw_midi_retains_source_tracks_and_timing(tmp_path):
    notes = [MelodyNote(60, 50, 123, .9, source='vocals'), MelodyNote(67, 1000, 400, .8, source='other')]
    export_midi(notes, tmp_path / 'tracks.mid', bpm=91)
    midi = mido.MidiFile(tmp_path / 'tracks.mid')
    assert len(midi.tracks) == 2
    assert 'vocals' in midi.tracks[0].name and 'other' in midi.tracks[1].name
    assert midi.length == pytest.approx(1.4, abs=.002)


def test_bounded_analysis_validation(tmp_path):
    result = layered_result()
    result.analysis['beats_ms'][2] = -1
    with pytest.raises(ValueError, match='拍点'):
        result.validate()


def test_all_uncertain_is_a_saved_rest_score():
    result = layered_result()
    result.confidence[:] = 0
    result.analysis['other_confidence'][:] = 0
    score, stats = build_score(result, 'Uncertain')
    assert not score.notes and stats['review'] > 0
    assert score.root['transcription']['sections']


def test_source_and_layer_controls_roundtrip(qtbot, tmp_path, monkeypatch):
    from test_transcription_ui import window_and_dialog
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    dialog.song = NeteaseSong('1', '分层样本', '', '', '')
    dialog.result = layered_result()
    dialog.remap()
    assert dialog.generation_mode.currentData() == 'auto'
    original = dialog.score.to_data()
    dialog.show_accompaniment.setChecked(False)
    assert all(n.extra['role'] == 'melody' for n in dialog.roll.score.notes)
    assert dialog.score.to_data() == original  # audition switches never delete score layers
    dialog.source_choice.setCurrentIndex(dialog.source_choice.findData('rest'))
    dialog.section_list.setCurrentIndex(0)
    dialog.apply_section_source()
    assert dialog.source_overrides and dialog.source_overrides[0]['source'] == 'rest'
    ident = dialog.draft_id
    dialog.close()
    window.transcribe()
    restored = window.transcription_dialog
    qtbot.addWidget(restored)
    restored.open_draft(restored.history_list.item(0))
    assert restored.draft_id == ident
    assert restored.source_overrides[0]['source'] == 'rest'
    assert not restored.show_accompaniment.isChecked()
    assert restored.result.analysis
    window.close()


def test_strict_snapping_cannot_fill_uncertain_sections():
    result = layered_result()
    score, _ = build_score(result, 'Boundaries', rhythm='strict', quantize=2, grid_offset_ms=75)
    uncertain = [s for s in score.root['transcription']['sections'] if s['needs_review']]
    for note in score.notes:
        assert not any(note.time_ms < s['end_ms'] and note.time_ms + note.duration_ms > s['start_ms'] for s in uncertain)


def test_chord_decoder_on_recorded_audio_features():
    cases = json.loads((ROOT / 'tests/fixtures/transcription/chord-audio-features.json').read_text())
    for case in cases:
        predictions = [chord_from_chroma(row) for row in case['chroma']]
        correct = sum(p is not None and sorted(p['pitches']) == case['expected_pcs'] for p in predictions)
        assert correct / len(predictions) >= .8, case['name']


@pytest.mark.parametrize("second_pitch", [60, 62])
def test_coarse_grid_collision_keeps_two_articulations(second_pitch):
    result = benchmark.fixture(dict(duration=1000, notes=[[60, 140, 80], [second_pitch, 230, 80]]))
    score, _ = build_score(result, 'Quick notes', mode='melody', rhythm='strict', quantize=2, shift=0)
    assert [n.time_ms for n in score.notes] == [140, 230]


def vocal_tail_result():
    result = benchmark.fixture(dict(duration=6000, notes=[[72, 1000, 1600], [74, 4000, 700]]))
    other = benchmark.fixture(dict(duration=6000, notes=[[67, 2350, 1000]]))
    result.analysis.update({f'other_{key}': getattr(other, key)
                            for key in ('frequency', 'confidence', 'energy_db', 'onset')})
    ratio = np.where(result.energy_db > -65, .8, .01)
    ratio[220:260] = .05  # voice is stable but accompanying instruments become louder
    result.analysis.update(vocal_ratio=ratio, other_ratio=np.full(600, .8),
                           mix_energy_db=np.full(600, -20.))
    return result


def test_established_vocal_tail_survives_instrumental_handover():
    score, _ = build_score(vocal_tail_result(), 'Tail', mode='melody', shift=0, rhythm='original')
    lead = next(n for n in score.notes if n.extra['source'] == 'vocals' and n.time_ms == 1000)
    assert lead.duration_ms == 1600
    assert all(n.time_ms >= 2600 for n in score.notes if n.extra['source'] == 'other')
    assert not any(n.extra['source'] == 'vocals' and 2600 <= n.time_ms < 4000 for n in score.notes)


def test_tail_protection_respects_manual_rest_and_missing_vocal_attack():
    result = vocal_tail_result()
    score, _ = build_score(result, 'Manual', mode='melody', rhythm='original',
                           overrides=[dict(start_ms=2400, end_ms=4000, source='rest')])
    assert next(n for n in score.notes if n.time_ms == 1000).duration_ms == 1400
    assert not any(n.time_ms < 4000 and n.time_ms + n.duration_ms > 2400 for n in score.notes)
    result.analysis['vocal_ratio'][:400] = .01
    notes, _ = arrange(result, mode='melody')
    assert not any(n.source == 'vocals' and n.start_ms < 4000 for n in notes)


@pytest.mark.parametrize('rhythm', ['original', 'gentle', 'strict'])
def test_bass_tail_at_beat_cannot_create_micro_note(rhythm):
    result = layered_result()
    bass = benchmark.fixture(dict(duration=12000, notes=[[48, 1500, 505], [50, 2070, 400]]))
    for key in ('frequency', 'confidence', 'energy_db', 'onset'):
        result.analysis['bass_' + key] = getattr(bass, key)
    result.analysis['chroma'][:] = 1  # no reliable chord; use direct bass evidence
    raw, _ = arrange(result)
    bass_notes = [n for n in raw if n.role == 'accompaniment']
    assert bass_notes and all(n.duration_ms >= 80 for n in bass_notes)
    assert bass_notes[0].start_ms == 2070  # retain the next attack, not a 5 ms old tail
    score, _ = build_score(result, 'Bass tails', rhythm=rhythm, quantize=4)
    assert all(n.duration_ms >= 80 - 1e-6 for n in score.notes if n.extra['role'] == 'accompaniment')


def test_accompaniment_rechecked_after_manual_section_clipping():
    result = layered_result()
    score, _ = build_score(result, 'Clipped', rhythm='strict', quantize=2, grid_offset_ms=-60,
                           overrides=[dict(start_ms=2085, end_ms=2500, source='rest')])
    assert all(n.duration_ms >= 80 - 1e-6 for n in score.notes if n.extra['role'] == 'accompaniment')
