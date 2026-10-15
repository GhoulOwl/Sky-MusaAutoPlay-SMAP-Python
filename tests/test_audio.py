import numpy as np

from autoskyplay.audio import INSTRUMENTS, Mixer


def test_all_instrument_assets_exist():
    mixer = Mixer()
    assert len(INSTRUMENTS) == 37
    for instrument in INSTRUMENTS:
        for key in range(15):
            assert (mixer.assets / instrument / f"{key}.wav").exists()


def test_polyphony_pitch_and_silence():
    mixer = Mixer()
    mixer.prepare("Piano", 12)
    mixer.trigger((0, 4, 7))
    data = mixer.render(4410)
    assert data.shape == (4410, 2)
    assert np.isfinite(data).all()
    assert np.any(data)
    assert np.max(np.abs(data)) <= 1
    mixer.silence()
    assert not np.any(mixer.render(200))


def test_sustain_release_and_chunked_reverb():
    mixer = Mixer()
    mixer.prepare("Flute")
    mixer.cave = True
    mixer.note_on(0)
    mixer.render(44100)
    assert mixer.voices
    mixer.note_off(0)
    assert np.isfinite(mixer.render(2205)).all()
    assert not mixer.voices


def test_automatic_sustain_obeys_duration_and_callback_boundaries():
    first, second = Mixer(), Mixer()
    for mixer in (first, second):
        mixer.prepare('Flute')
        mixer.trigger((0,), (200,))
    whole = first.render(15000)
    chunks = np.concatenate([second.render(3000) for _ in range(5)])
    np.testing.assert_allclose(whole, chunks, atol=1e-7)
    assert np.any(whole[6000:8000])
    assert not np.any(whole[10320:])
    assert not first.voices and not second.voices


def test_plucked_sample_is_not_time_stretched():
    first, second = Mixer(), Mixer()
    for mixer in (first, second):
        mixer.prepare('Piano')
    first.trigger((0,), (100,))
    second.trigger((0,), (1000,))
    np.testing.assert_equal(first.render(10000), second.render(10000))


def test_seek_restore_sustains_remaining_time_without_replaying_piano_attacks():
    mixer = Mixer()
    mixer.prepare('Flute')
    mixer.restore((0,), (200,))
    output = mixer.render(15000)
    assert np.any(output[6000:8000])
    assert not np.any(output[10320:])
    mixer.prepare('Piano')
    mixer.restore((0,), (200,))
    assert not mixer.voices
    assert not np.any(mixer.render(4410))
