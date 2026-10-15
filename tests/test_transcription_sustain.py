"""Known acoustic references for sustained tones versus real rearticulation."""
import numpy as np
import pytest
import soundfile as sf

from autoskyplay.transcription.boundaries import enrich_boundaries, stem_evidence
from autoskyplay.transcription.melody import PitchResult, to_score
from autoskyplay.transcription.segmentation import decode

RATE = 16000


def acoustic_result(tmp_path, attacks=(430, 960, 1700), *, kind='held'):
    t = np.arange(2 * RATE) / RATE
    frequency = 440 * 2 ** (.15 * np.sin(2 * np.pi * 5 * t) / 12)
    phase = np.cumsum(frequency) * 2 * np.pi / RATE
    harmonic = np.full(len(t), .2)
    amplitude = np.full(len(t), .2)
    if kind == 'timbre':
        # Equal-RMS change of vowel-like harmonic envelope; no energy valley.
        harmonic[t >= 1] = 1.8
    if kind == 'reattack':
        amplitude[(t >= .94) & (t < 1)] = .04
    if kind == 'gradual':
        harmonic = .2 + .15 * t
    audio = amplitude * (np.sin(phase) + harmonic * np.sin(phase * 3)) / np.sqrt(1 + harmonic ** 2)
    path = tmp_path / 'vocals.wav'
    sf.write(path, audio, RATE, subtype='FLOAT')
    times = np.arange(200) * .01
    f0 = 440 * 2 ** (.15 * np.sin(2 * np.pi * 5 * times) / 12)
    # Actual centered RMS, independent of the segmentation implementation.
    energy = [20 * np.log10(max(1e-8, np.sqrt(np.mean(audio[max(0, i * 160 - 320):
                                                            min(len(audio), i * 160 + 320)] ** 2))))
              for i in range(200)]
    onset = np.zeros(200)
    onset[np.array(attacks, dtype=int) // 10] = 1
    result = PitchResult(f0, np.full(200, .95), np.array(energy), onset, duration_ms=2000)
    return result, path


@pytest.mark.parametrize('kind', ['held', 'gradual'])
def test_false_onsets_in_continuous_audio_do_not_retrigger(tmp_path, kind):
    result, path = acoustic_result(tmp_path, kind=kind)
    original = [getattr(result, n).copy() for n in ('frequency', 'confidence', 'energy_db', 'onset')]
    assert len(result.notes()) == 4  # legacy flags alone are explicitly ambiguous
    result.analysis['boundary_change'] = stem_evidence(path, result)
    audit = []
    notes = decode(result, .35, 80, audit=audit)
    assert [(n.pitch, n.start_ms, n.duration_ms) for n in notes] == [(69, 0, 2000)]
    assert [a['decision'] for a in audit] == ['sustain'] * 3
    for name, expected in zip(('frequency', 'confidence', 'energy_db', 'onset'), original):
        np.testing.assert_array_equal(getattr(result, name), expected)


@pytest.mark.parametrize('kind', ['reattack', 'timbre'])
def test_real_same_pitch_rearticulation_survives_with_and_without_energy_dip(tmp_path, kind):
    result, path = acoustic_result(tmp_path, attacks=(1000,), kind=kind)
    result.analysis['boundary_change'] = stem_evidence(path, result)
    if kind == 'timbre':
        assert np.ptp(result.energy_db[94:107]) < 3
        assert result.analysis['boundary_change'][100] >= .01
    assert [(n.start_ms, n.duration_ms) for n in result.notes()] == [(0, 1000), (1000, 1000)]


def test_periodic_weak_reattacks_keep_their_rhythm(tmp_path):
    result, _ = acoustic_result(tmp_path, attacks=(400, 800, 1200, 1600))
    enrich_boundaries(result, tmp_path)
    assert len(result.notes()) == 5


@pytest.mark.parametrize('duration', [10, 30, 70, 150])
def test_breath_and_silence_are_not_joined(tmp_path, duration):
    result, _ = acoustic_result(tmp_path, attacks=())
    result.analysis['boundary_change'] = np.zeros(200)
    left, right = 90, 90 + duration // 10
    result.confidence[left:right] = 0
    result.energy_db[left:right] = -100
    notes = result.notes()
    assert [(n.start_ms, n.start_ms + n.duration_ms) for n in notes] == [(0, 900), (right * 10, 2000)]


def test_uncertain_confidence_break_is_not_converted_to_an_attack_veto(tmp_path):
    result, _ = acoustic_result(tmp_path, attacks=(1000,))
    result.analysis['boundary_change'] = np.zeros(200)
    result.confidence[98:101] = .4
    assert len(result.notes()) == 2
    result.confidence[96:104] = 0
    assert [(n.start_ms, n.duration_ms) for n in result.notes()] == [(0, 960), (1040, 960)]


def test_different_source_pitches_mapping_to_one_key_are_not_joined(tmp_path):
    result, _ = acoustic_result(tmp_path, attacks=())
    result.frequency[:100] = 440 * 2 ** ((60 - 69) / 12)
    result.frequency[100:] = 440 * 2 ** ((61 - 69) / 12)
    result.analysis['boundary_change'] = np.zeros(200)
    notes = result.notes()
    score, _ = to_score(notes, 'mapped', shift=0)
    assert len(notes) == len(score.notes) == 2
    assert [n.extra['sourcePitch'] for n in score.notes] == [60, 61]


def test_evidence_cache_survives_stem_eviction_and_rejects_wrong_pitch_file(tmp_path):
    result, path = acoustic_result(tmp_path)
    np.savez_compressed(tmp_path / 'pitch.npz', **result.arrays())
    raw = (tmp_path / 'pitch.npz').read_bytes()
    enrich_boundaries(result, tmp_path, cache=True)
    assert len(result.notes()) == 1
    assert (tmp_path / 'pitch.npz').read_bytes() == raw
    path.unlink()
    restored = enrich_boundaries(PitchResult.load(tmp_path / 'pitch.npz'), tmp_path, cache=True)
    assert len(restored.notes()) == 1
    restored.onset[50] = 1
    restored.analysis = {}
    np.savez_compressed(tmp_path / 'pitch.npz', **restored.arrays())
    wrong = enrich_boundaries(PitchResult.load(tmp_path / 'pitch.npz'), tmp_path, cache=True)
    assert 'boundary_change' not in wrong.analysis


@pytest.mark.parametrize('payload', [b'broken zip', b'PK\x03\x04\xff'])
def test_corrupt_evidence_cache_does_not_break_old_draft(tmp_path, payload):
    result, path = acoustic_result(tmp_path)
    path.unlink()
    np.savez_compressed(tmp_path / 'pitch.npz', **result.arrays())
    (tmp_path / 'boundary-evidence.npz').write_bytes(payload)
    enrich_boundaries(result, tmp_path, cache=True)
    assert len(result.notes()) == 4


def test_track_evidence_roundtrip_and_cancellation(tmp_path):
    result, _ = acoustic_result(tmp_path)
    with pytest.raises(ValueError, match='取消'):
        enrich_boundaries(result, tmp_path, cancelled=lambda: True)
    enrich_boundaries(result, tmp_path)
    for key in ('frequency', 'confidence', 'energy_db', 'onset'):
        result.analysis['other_' + key] = getattr(result, key).copy()
    result.analysis['other_boundary_change'] = result.analysis['boundary_change'].copy()
    np.savez_compressed(tmp_path / 'pitch.npz', **result.arrays())
    restored = PitchResult.load(tmp_path / 'pitch.npz')
    assert len(restored.track('other').notes()) == 1
    restored.analysis['boundary_change'][100] = 3
    with pytest.raises(ValueError, match='起音'):
        restored.validate()
