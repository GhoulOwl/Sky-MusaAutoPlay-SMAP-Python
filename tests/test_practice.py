from autoskyplay.practice import Practice
from autoskyplay.score import Note, Score


def test_chord_must_be_held_together_and_rearticulated():
    practice = Practice(Score(notes=[Note(0, 0), Note(4, 0), Note(0, 500), Note(4, 500)]))
    assert not practice.press(0)
    practice.release(0)
    assert not practice.press(4)
    assert practice.press(0)
    assert practice.index == 1
    assert not practice.press(0)  # Key repeat cannot advance the next chord.
    practice.release(0)
    practice.release(4)
    assert not practice.press(0)
    assert practice.press(4)
    assert practice.finished
