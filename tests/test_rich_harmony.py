from copy import deepcopy

import numpy as np
import pytest

from autoskyplay.audio import INSTRUMENTS
from autoskyplay.midi import SKY_MIDI
from autoskyplay.score import Note, Score
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.events import EventResult, ModelNote
from autoskyplay.transcription.harmonic_structure import pulse_from_evidence
from autoskyplay.transcription.local import MidiResult
from autoskyplay.transcription.melody import MelodyNote
from autoskyplay.transcription.performance import generation_mode
from autoskyplay.transcription.rich_harmony import rich_accompaniment, sounding_end, synchronize_attacks


def layered():
    notes = []
    for t in range(0, 8000, 500):
        # Root + third identifies the missing fifth only with explicit bass evidence.
        for pitch, instrument in [(48, 'acoustic_bass'), (64, 'piano')]:
            notes.append(ModelNote(pitch, t, 480, instrument, len(notes)))
        if 2000 <= t < 6000:
            notes.append(ModelNote(76, t, 420, 'voice', len(notes)))
    return EventResult(notes, 8000, 120, {}, dict(
        beats_ms=list(range(0, 8000, 500)), beat_confidence=.9, energy_db=[-20]*80))


def lead_identity(score):
    return [(n.key, n.time_ms, n.duration_ms, n.extra['sourcePitch'])
            for n in score.notes if n.extra['role'] == 'melody']


def assert_three_keys(score):
    for t in sorted({n.time_ms for n in score.notes}):
        active = [n for n in score.notes if n.time_ms <= t < n.time_ms+n.duration_ms]
        assert len(active) <= 3
        assert len({n.key for n in active}) == len(active)


def test_rich_preserves_lead_and_completes_only_supported_harmony():
    result = layered()
    raw = deepcopy(result.raw_notes)
    lead, _ = build_score(result, 'lead', mode='melody', shift=0)
    rich, _ = build_score(result, 'rich', mode='rich_chords', shift=0)
    sparse, _ = build_score(result, 'old', mode='chords', shift=0)
    assert lead_identity(lead) == lead_identity(rich) == lead_identity(sparse)
    assert result.raw_notes == raw
    assert len(rich.notes) > len(sparse.notes)
    generated = [n for n in rich.notes if n.extra['generated']]
    assert generated and all(n.extra['evidenceEvents'] for n in generated)
    assert all(n.extra['harmonyRoot'] == 0 for n in generated)
    assert_three_keys(rich)


def test_independent_harmony_survives_uncertain_lead_intro_interlude_outro():
    # Equally strong sources make the lead ambiguous; harmony must remain independent.
    result = EventResult([ModelNote(p, t, 450, instrument, i*3+j)
                          for i, t in enumerate(range(0, 6000, 500))
                          for j, (p, instrument) in enumerate([(48, 'acoustic_bass'), (64, 'piano'), (67, 'guitar')])],
                         6000, 120, {}, dict(energy_db=[-20]*60, beat_confidence=.9,
                                             beats_ms=list(range(0, 6000, 500))))
    rich, _ = build_score(result, 'instrumental', mode='rich_chords', shift=0)
    for a, b in [(0, 2000), (2000, 4000), (4000, 6000)]:
        assert any(a <= n.time_ms < b and n.extra['role'] == 'accompaniment' for n in rich.notes)
    assert any(s['needs_review'] for s in rich.root['transcription']['sections'])


def test_silence_and_manual_rests_block_all_added_notes():
    result = layered()
    result.analysis['energy_db'][20:30] = [-100]*10
    rich, _ = build_score(result, 'rests', mode='rich_chords', shift=0,
                          overrides=[dict(start_ms=4500, end_ms=5500, source='rest')])
    for n in rich.notes:
        if n.extra['role'] == 'accompaniment':
            assert not (n.time_ms < 3000 and n.time_ms+n.duration_ms > 2000)
        assert not (n.time_ms < 5500 and n.time_ms+n.duration_ms > 4500)


def test_unreliable_rhythm_never_invents_subdivision_or_unknown_chord():
    result = layered()
    result.analysis['beat_confidence'] = 0
    result.raw_notes = [n for n in result.raw_notes if n.instrument != 'piano']
    rich, _ = build_score(result, 'weak', mode='rich_chords', shift=0)
    assert not any(n.extra['generated'] for n in rich.notes)
    assert all(n.time_ms in {x.start_ms for x in result.raw_notes} for n in rich.notes)
    assert rich.root['transcription']['harmony_review']


@pytest.mark.parametrize('instrument', INSTRUMENTS)
def test_every_instrument_is_selectable_and_obeys_key_budget(instrument):
    assert generation_mode('rich_chords', instrument) == 'rich_chords'
    rich, _ = build_score(layered(), instrument, mode='rich_chords', instrument=instrument, shift=0)
    assert_three_keys(rich)
    assert rich.root['transcription']['clarity']['policy'] == 'rich-harmony-4'


def test_midi_timing_and_default_modes_are_unchanged():
    source = MidiResult([MelodyNote(p, t, 200, 1, source='midi:1')
                         for t in (0, 250, 500, 750) for p in (60, 64, 67, 76)], 1000, 120)
    before = deepcopy(source.raw_notes)
    lead, _ = build_score(source, 'lead', mode='melody', rhythm='original', shift=0)
    rich, _ = build_score(source, 'rich', mode='rich_chords', rhythm='original', shift=0)
    assert lead_identity(rich) == lead_identity(lead)
    assert source.raw_notes == before
    assert generation_mode('auto', 'Piano') == 'chords'
    assert generation_mode('auto', 'Violin') == 'melody'
    assert_three_keys(rich)


def test_repeated_attacks_long_notes_and_chromatic_lead_survive():
    result = layered()
    result.raw_notes = [n for n in result.raw_notes if n.instrument != 'voice']
    result.raw_notes += [ModelNote(p, t, d, 'voice', 100+i)
                         for i, (p, t, d) in enumerate([(73, 2000, 1200), (73, 3200, 200),
                                                       (76, 3500, 150), (77, 3750, 150),
                                                       (79, 4000, 1500)])]
    lead, _ = build_score(result, 'lead', mode='melody', shift=0)
    rich, _ = build_score(result, 'rich', mode='rich_chords', shift=0)
    assert lead_identity(rich) == lead_identity(lead)
    assert_three_keys(rich)


def test_rich_refinement_protects_outside_notes_and_keeps_provenance():
    from dataclasses import asdict

    from test_refinement import local_for

    from autoskyplay.transcription.refinement import Region, make_candidate

    result = layered()
    score, _ = build_score(result, 'rich', mode='rich_chords', shift=0)
    region = Region.create(result, 2000, 6000)
    params = dict(instrument='Piano', mode='rich_chords', minimum=80, shift=0, overrides=[])
    candidate = make_candidate('a'*32, result, score, params, local_for(result, region), region)
    assert not candidate.conflicts
    assert [asdict(n) for n in candidate.score.notes if n.time_ms < 2000 or n.time_ms >= 6000] == [
        asdict(n) for n in score.notes if n.time_ms < 2000 or n.time_ms >= 6000]
    assert all(n.extra.get('evidenceEvents') for n in candidate.score.notes if n.extra.get('generated'))


def test_rich_ui_mode_roundtrip_and_native_ornament_hint(qtbot, tmp_path, monkeypatch):
    from test_transcription_ui import window_and_dialog

    from autoskyplay.netease import NeteaseSong

    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    dialog.song = NeteaseSong('1', 'Rich', '', '', '')
    dialog.result = layered()
    dialog.generation_mode.setCurrentIndex(dialog.generation_mode.findData('rich_chords'))
    dialog.remap()
    assert dialog.parameters()['mode'] == 'rich_chords'
    assert not dialog.ornaments.isEnabled() and '不适用' in dialog.ornaments.toolTip()
    before = dialog.draft_id
    dialog.edited = True
    dialog.persist_draft()
    dialog.generation_mode.setCurrentIndex(dialog.generation_mode.findData('melody'))
    dialog.flush_remap()
    assert dialog.draft_id != before
    assert dialog.drafts.load(before)[3].root['transcription']['mode'] == 'rich_chords'
    dialog.close()
    window.close()


def balance_fixture(lead, events, instrument='Piano', duration=4000):
    score = Score(bpm=120, notes=lead, root={'transcription': dict(
        instrument=instrument, shift=0, warnings=[], beats_ms=list(range(0, duration, 500)))})
    result = EventResult(events, duration, 120, {}, dict(
        beats_ms=list(range(0, duration, 500)), beat_confidence=.9, energy_db=[-20]*(duration//100)))
    before = deepcopy(lead)
    rich_accompaniment(score, result)
    assert [n for n in score.notes if n.extra['role'] == 'melody'] == before
    return score


def assert_balanced(score):
    lead = [n for n in score.notes if n.extra['role'] == 'melody']
    support = [n for n in score.notes if n.extra['role'] == 'accompaniment']
    for n in support:
        for m in lead:
            if min(sounding_end(n), m.time_ms+m.duration_ms)-max(n.time_ms, m.time_ms) > 1e-3:
                assert SKY_MIDI[m.key]-SKY_MIDI[n.key] >= 3
    for t in {n.time_ms for n in score.notes}:
        active = [n for n in support if n.time_ms <= t and sounding_end(n) > t+1e-3]
        melody = [n for n in lead if n.time_ms <= t and n.time_ms+n.duration_ms > t+1e-3]
        assert len(active) <= (2 if melody else 3)
        assert len(active)+len(melody) <= 3
        assert len({n.key for n in active}) == len(active)


def melody(key, time, duration, source='vocals'):
    return Note(key, time, duration, dict(role='melody', source=source, sourceTime=time,
                                         sourcePitch=SKY_MIDI[key]))


@pytest.fixture
def uniform_energy(monkeypatch):
    # Isolate register/timing policy from differences in the recorded sample bank.
    monkeypatch.setattr('autoskyplay.transcription.rich_harmony.sample_energy',
                        lambda instrument: tuple((np.array([0.]), np.array([0.]), 1.) for _ in range(15)))


def test_low_melody_never_pushes_bass_to_upper_octave():
    # Previously C4's tail collided with the following C4 lead, rescuing it as C6.
    events = [ModelNote(p, 0, 400, 'piano', i) for i, p in enumerate((48, 64, 67))]
    score = balance_fixture([melody(2, 0, 400), melody(0, 400, 1000)], events)
    assert_balanced(score)
    assert not [n for n in score.notes if n.extra['role'] == 'accompaniment']


@pytest.mark.parametrize('instrument', ['Piano', 'Guitar', 'Violin', 'Flute'])
def test_dense_original_events_share_tail_and_attack_budget(instrument, uniform_energy):
    events = [ModelNote(p, t, 95, 'piano', i*6+j)
              for i, t in enumerate(range(0, 4000, 100))
              for j, p in enumerate((48, 60, 64, 67, 76, 79))]
    score = balance_fixture([melody(14, 0, 4000)], events, instrument)
    support = [n for n in score.notes if n.extra['role'] == 'accompaniment']
    assert 1 <= len(support) <= 2
    assert {n.time_ms for n in support} == {0}  # No new attacks underneath a held lead.
    assert_balanced(score)
    assert score.root['transcription']['clarity']['harmonic_spans'] == 1


def test_short_breath_does_not_introduce_an_unanchored_attack(uniform_energy):
    score = balance_fixture([melody(6, 0, 400), melody(4, 1000, 1000)],
                            [ModelNote(76, 600, 200, 'piano', 0)])
    support = [n for n in score.notes if n.extra['role'] == 'accompaniment']
    assert not support
    assert_balanced(score)


def test_sustained_support_releases_before_lower_melody_returns(uniform_energy):
    score = balance_fixture([melody(14, 0, 500), melody(3, 500, 1500)],
                            [ModelNote(76, 0, 1600, 'piano', 0)], 'Violin')
    support = [n for n in score.notes if n.extra['role'] == 'accompaniment']
    assert support and max(sounding_end(n) for n in support) <= 500+1e-3
    assert_balanced(score)


def test_decayed_melody_does_not_invite_a_louder_new_support_attack(monkeypatch):
    # A four-second piano note on paper has already decayed after 200 ms.
    envelope = (np.array([0., 100., 200., 2000.]), np.array([0., 80., 90., 90.]), 0.)
    monkeypatch.setattr('autoskyplay.transcription.rich_harmony.sample_energy', lambda instrument: (envelope,)*15)
    score = balance_fixture([melody(14, 0, 4000)],
                            [ModelNote(64, 1500, 200, 'piano', 0)])
    assert not [n for n in score.notes if n.extra['role'] == 'accompaniment']
    assert score.root['transcription']['clarity']['rejected']['no_safe_melody_anchor'] > 0


def test_all_instruments_protect_the_lead_register_and_strong_tails():
    for instrument in INSTRUMENTS:
        score, _ = build_score(layered(), instrument, mode='rich_chords', instrument=instrument, shift=0)
        assert_balanced(score)


def test_refinement_rejects_protected_old_support_above_new_melody():
    from test_refinement import local_for

    from autoskyplay.transcription.refinement import Region, make_candidate

    result = layered()
    score, _ = build_score(result, 'edited old draft', mode='rich_chords', shift=0)
    protected = Note(14, 1800, 900, dict(role='accompaniment', strongDuration=900))
    score.notes.append(protected)
    score.notes.sort(key=lambda n: (n.time_ms, n.key))
    region = Region.create(result, 2000, 6000)
    candidate = make_candidate('a'*32, result, score, dict(instrument='Piano', mode='rich_chords',
                               minimum=80, shift=0, overrides=[]), local_for(result, region), region)
    assert any('侵入新旋律音区' in reason for reason in candidate.conflicts)
    assert protected in candidate.score.notes


def test_phrase_plan_cannot_override_register_or_tail_protection(monkeypatch):
    monkeypatch.setattr('autoskyplay.transcription.rich_harmony.voice_path',
                        lambda layers, target: {row[0]: 84 for row in layers})
    for instrument in ('Piano', 'Guitar', 'Violin', 'Flute'):
        score, _ = build_score(layered(), 'plan guard', mode='rich_chords', instrument=instrument, shift=0)
        assert_balanced(score)
        assert_three_keys(score)


def test_supported_chords_with_unreliable_beats_keep_source_onsets():
    result = layered()
    result.analysis['beat_confidence'] = .1
    score, _ = build_score(result, 'source rhythm', mode='rich_chords', rhythm='original', shift=0)
    support = [n for n in score.notes if n.extra['role'] == 'accompaniment']
    assert support and all(n.time_ms in {r.start_ms for r in result.raw_notes} for n in support)
    assert all(n.extra['rhythmicRole'] == 'source' for n in support)
    assert score.root['transcription']['harmony_rhythm']['strong_beats_ms'] == []


def test_observed_strong_and_weak_pulses_choose_bass_and_single_inner_voices(uniform_energy):
    from test_harmonic_structure import accent_evidence

    events = [ModelNote(n.pitch, n.start_ms, 180, n.instrument, i)
              for i, n in enumerate(accent_evidence())]
    events.append(ModelNote(48, 0, 10000, 'acoustic_bass', 99999))
    # Pulse roles alternate only on actual lead attacks.
    score = balance_fixture([melody(12, t, 450) for t in range(0, 10000, 500)],
                            events, 'Violin', duration=10000)
    support = [n for n in score.notes if n.extra['role'] == 'accompaniment']
    weak = [n for n in support if n.extra['rhythmicRole'] == 'pulse' and n.extra.get('harmonyRoot') is not None]
    assert weak and all(n.extra['voice'] == 'inner' for n in weak)
    assert len({n.time_ms for n in weak}) == len(weak)
    assert any(n.extra['rhythmicRole'] == 'strong' for n in support)
    assert_balanced(score)


def test_accompaniment_uses_final_melody_onset_and_preserves_raw_provenance(uniform_energy):
    lead = melody(12, 1000, 1000)
    lead.extra['sourceTime'] = 1040  # The mapped lead has already snapped to 1000.
    events = [ModelNote(p, 1030, 400, 'piano', i) for i, p in enumerate((48, 64, 67))]
    raw = deepcopy(events)
    score = balance_fixture([lead], events)
    support = [n for n in score.notes if n.extra['role'] == 'accompaniment']
    assert support and {n.time_ms for n in support} == {1000}
    assert all(n.extra['sourceTime'] == 1030 and n.extra['sourceDuration'] == 400
               and n.extra['alignmentOffsetMs'] == -30 for n in support if not n.extra['generated'])
    assert events == raw
    assert_balanced(score)


def aligned_candidates(lead, events, blocked=(), chord_at=lambda _: None, pulse=None):
    pulse = pulse or pulse_from_evidence({}, [], 500)
    return synchronize_attacks([(n, chord_at(n.start_ms)) for n in events],
                               lead, pulse, blocked, chord_at, 80)


def test_alignment_preserves_independent_passages_but_not_attacks_under_held_lead():
    lead = [melody(12, 1000, 2000), melody(12, 3500, 500), melody(12, 8000, 1000)]
    events = [MelodyNote(60, t, 500, 1, source='other') for t in (0, 1050, 2000, 3200, 6000, 10000)]
    aligned, origins, rejected = aligned_candidates(lead, events)
    assert [n.start_ms for n, _c in aligned] == [0, 1000, 6000, 10000]
    assert [origins[id(n)][1] for n, _c in aligned] == ['independent', 'melody', 'independent', 'independent']
    assert rejected['no_safe_melody_anchor'] == 2


@pytest.mark.parametrize('barrier', ['rest', 'chord', 'duration'])
def test_alignment_does_not_cross_rests_chord_changes_or_pad_short_evidence(barrier):
    lead = [melody(12, 1000, 1000)]
    event = MelodyNote(60, 900, 150 if barrier == 'duration' else 500, 1, source='other')
    chord_at = (lambda t: dict(root=0 if t < 950 else 5, quality='major', end_ms=3000)) if barrier == 'chord' else lambda _: None
    aligned, _origins, rejected = aligned_candidates(lead, [event],
        blocked=[(940, 960)] if barrier == 'rest' else [], chord_at=chord_at)
    assert not aligned and rejected['no_safe_melody_anchor'] == 1


def test_alignment_at_end_of_rest_and_close_melody_attacks_remain_distinct():
    lead = [melody(12, 1000, 20), melody(13, 1030, 100)]
    events = [MelodyNote(60, t, 100, 1, source='other') for t in (1001, 1031)]
    aligned, _origins, rejected = aligned_candidates(lead, events, [(800, 1000)])
    assert [n.start_ms for n, _c in aligned] == [1000, 1030]
    assert not rejected


def test_alignment_limit_follows_fast_local_tempo_and_breaks_ties_earlier():
    pulse = pulse_from_evidence(dict(beats_ms=list(range(0, 2000, 200)), beat_confidence=.9), [], 500)
    lead = [melody(12, 400, 500), melody(13, 1000, 500), melody(14, 1200, 500)]
    events = [MelodyNote(60, t, 500, 1) for t in (550, 1100)]
    aligned, _origins, rejected = aligned_candidates(lead, events, pulse=pulse)
    assert [n.start_ms for n, _c in aligned] == [1000]
    assert rejected['no_safe_melody_anchor'] == 1


def test_three_key_limit_accounts_for_overlapping_melody_and_future_reentry(uniform_energy):
    events = [ModelNote(p, 0, 1500, 'piano', i) for i, p in enumerate((48, 64, 67))]
    score = balance_fixture([melody(12, 0, 2000), melody(14, 0, 2000)], events, 'Violin')
    support = [n for n in score.notes if n.extra['role'] == 'accompaniment']
    assert support and len(support) == 1
    assert_three_keys(score)
    # A third held melody key enters while any new support would still sound.
    score = balance_fixture([melody(12, 0, 2000), melody(14, 0, 2000), melody(10, 300, 1700)],
                            events, 'Violin')
    assert all(sounding_end(n) <= 300+1e-3 for n in score.notes if n.extra['role'] == 'accompaniment')
    assert_balanced(score)


def test_midi_lead_already_over_three_keys_is_reported_without_editing_it():
    from autoskyplay.score import ScoreError

    lead = [melody(k, i*100, 1000, 'midi:1') for i, k in enumerate((9, 10, 12, 14))]
    before = deepcopy(lead)
    with pytest.raises(ScoreError, match='主旋律本身超过三键'):
        balance_fixture(lead, [], 'Violin')
    assert lead == before


def test_refinement_preserves_old_three_voice_boundary_and_reports_fourth_key_conflict():
    from test_refinement import local_for

    from autoskyplay.transcription.refinement import Region, make_candidate

    result = layered()
    score, _ = build_score(result, 'old draft', mode='rich_chords', shift=0)
    score.notes = [n for n in score.notes if n.extra['role'] != 'accompaniment']
    protected = [Note(k, 1900, 800, dict(role='accompaniment', strongDuration=800)) for k in (0, 1, 4)]
    score.notes = sorted(score.notes+protected, key=lambda n: (n.time_ms, n.key))
    region = Region.create(result, 2000, 6000)
    candidate = make_candidate('a'*32, result, score, dict(instrument='Violin', mode='rich_chords',
        minimum=80, shift=0, overrides=[]), local_for(result, region), region)
    assert any('超过三键' in reason for reason in candidate.conflicts)
    assert all(n in candidate.score.notes for n in protected)
