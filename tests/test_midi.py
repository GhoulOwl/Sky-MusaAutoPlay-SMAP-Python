import mido
import pytest

from autoskyplay.midi import MidiImporter
from autoskyplay.score import ScoreError


def test_conductor_tempo_applies_to_selected_melody(tmp_path):
    midi = mido.MidiFile(ticks_per_beat=480)
    conductor = mido.MidiTrack()
    conductor.extend(
        [
            mido.MetaMessage("set_tempo", tempo=500_000, time=0),
            mido.MetaMessage("set_tempo", tempo=1_000_000, time=480),
        ]
    )
    melody = mido.MidiTrack()
    melody.extend(
        [
            mido.Message("note_on", note=60, velocity=80, time=0),
            mido.Message("note_off", note=60, time=480),
            mido.Message("note_on", note=64, velocity=80, time=0),
            mido.Message("note_off", note=64, time=480),
            mido.Message("note_on", note=67, velocity=80, time=0),
        ]
    )
    midi.tracks.extend([conductor, melody])
    path = tmp_path / "tempo.mid"
    midi.save(path)
    score = MidiImporter(path).convert({1}, shift=0)
    assert [n.time_ms for n in score.notes] == [0, 500, 1500]
    assert score.notes[1].duration_ms == 1000
    assert [n.key for n in score.notes] == [0, 2, 4]


def test_type_two_is_rejected(tmp_path):
    path = tmp_path / "type2.mid"
    midi = mido.MidiFile(type=2)
    midi.tracks.append(mido.MidiTrack())
    midi.save(path)
    with pytest.raises(ScoreError, match="type 0/1"):
        MidiImporter(path)
