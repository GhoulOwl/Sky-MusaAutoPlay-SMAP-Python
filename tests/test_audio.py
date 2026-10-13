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
