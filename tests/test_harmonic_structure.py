from copy import deepcopy

import numpy as np

from autoskyplay.transcription.events import EventResult
from autoskyplay.transcription.harmonic_structure import pulse_from_evidence, voice_path
from autoskyplay.transcription.melody import MelodyNote
from autoskyplay.transcription.rich_harmony import harmony_windows


def harmonic_result(rows):
    return EventResult([], len(rows)*500, 120, {}, dict(
        beats_ms=list(range(0, (len(rows)+1)*500, 500)), beat_confidence=.9,
        chroma=rows, harmony_energy_db=[-20]*len(rows)))


def chroma(**tones):
    row = np.zeros(12)
    for pitch, level in tones.items():
        row[int(pitch)] = level
    return row.tolist()


C = chroma(**{'0': 1, '4': 1, '7': 1})
F = chroma(**{'5': 1, '9': 1, '0': 1})
AMBIGUOUS = chroma(**{'0': 1, '4': 1, '7': .90, '9': 1})


def test_supported_harmonic_continuity_resolves_flicker_without_changing_evidence():
    result = harmonic_result([C, AMBIGUOUS, C])
    saved = deepcopy(result.analysis)
    chords, _reviews, _ = harmony_windows(result, [], [])
    assert [c['root'] for c in chords] == [0, 0, 0]
    assert [c['continuity_resolved'] for c in chords] == [False, True, False]
    assert result.analysis == saved


def test_continuity_cannot_override_a_clear_chord_change_or_create_unknown_harmony():
    result = harmonic_result([C, F, C, [0]*12, AMBIGUOUS])
    chords, _reviews, _ = harmony_windows(result, [], [])
    assert [(c['start_ms'], c['root']) for c in chords] == [(0, 0), (500, 5), (1000, 0)]


def test_ambiguous_material_without_an_anchor_stays_unresolved():
    chords, _reviews, _ = harmony_windows(harmonic_result([AMBIGUOUS]*4), [], [])
    assert chords == []


def test_rest_breaks_harmonic_continuity():
    result = harmonic_result([C, AMBIGUOUS, AMBIGUOUS])
    chords, _reviews, _ = harmony_windows(result, [], [(500, 1000)])
    assert [(c['start_ms'], c['end_ms']) for c in chords] == [(0, 500)]


def accent_evidence():
    notes = []
    for i in range(20):
        pitches = (48, 64, 67) if i % 4 == 0 else (64,)
        for p in pitches:
            notes.append(MelodyNote(p, i*500, 400, 1,
                                   instrument='acoustic_bass' if p == 48 else 'piano'))
    return notes


def test_observed_accents_use_local_pulse_and_do_not_invent_downbeats():
    analysis = dict(beats_ms=list(range(0, 10000, 500)), beat_confidence=.9)
    pulse = pulse_from_evidence(analysis, accent_evidence(), 500)
    assert pulse.at(2000)[0] == 'strong'
    assert pulse.at(2500)[0] == 'pulse'
    assert pulse.at(2250)[0] == 'offbeat'
    assert pulse.at(11000)[0] == 'source'
    assert pulse.metadata()['inferred_meter'] is None
    analysis['beats_ms'][5] = 2600
    varying = pulse_from_evidence(analysis, accent_evidence(), 500)
    assert varying.at(2100)[1] == 600


def test_uniform_or_unreliable_beats_do_not_get_arbitrary_strong_labels():
    analysis = dict(beats_ms=list(range(0, 10000, 500)), beat_confidence=.9)
    uniform = [n for n in accent_evidence() if n.pitch == 64]
    assert pulse_from_evidence(analysis, uniform, 500).at(2000)[0] == 'pulse'
    analysis['beat_confidence'] = .2
    assert pulse_from_evidence(analysis, accent_evidence(), 500).at(2000)[0] == 'source'


def test_voice_path_looks_ahead_and_resets_between_phrases():
    layers = [(0, 0, 0, 500, [(60, 0), (72, 0)]),
              (1, 0, 500, 500, [(71, 0)]), (2, 0, 1000, 500, [(72, 0)]),
              (3, 1, 1500, 500, [(60, 0), (72, 0)])]
    assert voice_path(layers, 62) == {0: 72, 1: 71, 2: 72, 3: 60}
    layers[-1] = (3, 0, 4000, 500, [(60, 0), (72, 0)])
    assert voice_path(layers, 62)[3] == 60


def test_voice_path_is_deterministic_and_never_invents_an_option():
    layers = [(i, 0, i*500, 500, [(60+i % 7, .1), (72+i % 7, .2)]) for i in range(20)]
    first = voice_path(layers, 62)
    assert first == voice_path(layers, 62)
    assert all(first[i] in {p for p, _ in options} for i, _, _, _, options in layers)
