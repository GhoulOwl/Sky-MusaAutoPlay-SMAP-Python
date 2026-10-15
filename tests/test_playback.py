import threading
import time

import pytest

from autoskyplay.playback import Player, State, Timeline
from autoskyplay.score import Note, Score


class Output:
    def __init__(self):
        self.calls = []
        self.silenced = 0
        self.fired = threading.Event()
        self.restores = []
        self.restored = threading.Event()

    def trigger(self, keys, durations):
        self.calls.append(keys)
        self.fired.set()

    def pump(self):
        pass

    def restore(self, keys, durations):
        self.restores.append((keys, durations))
        self.restored.set()

    def silence(self):
        self.silenced += 1


def test_chords_and_speed_on_song_clock():
    timeline = Timeline(Score(notes=[Note(0, 0), Note(4, 0), Note(1, 500), Note(2, 1000)]))
    groups = timeline.advance(0, 1)
    assert [[n.key for n in g] for g in groups] == [[0, 4]]
    assert timeline.advance(200, 2) == []
    assert [n.key for n in timeline.advance(100, 1)[0]] == [1]
    timeline.seek(1000)
    assert [n.key for n in timeline.advance(0, 1)[0]] == [2]


def test_stop_releases_without_natural_completion():
    output, done = Output(), threading.Event()
    player = Player(on_done=done.set)
    player.load(Score(notes=[Note(0, 0), Note(1, 10_000)]))
    player.play(output)
    assert output.fired.wait(1)
    player.stop()
    assert output.silenced > 0
    assert not done.is_set()
    assert player.state == State.STOPPED


def test_paused_seek_does_not_emit_until_resume():
    output = Output()
    player = Player()
    player.load(Score(notes=[Note(0, 0), Note(1, 10_000), Note(2, 20_000)]))
    player.play(output)
    assert output.fired.wait(1)
    player.pause()
    player.seek(20_000)
    time.sleep(0.05)
    assert output.calls == [(0,)]
    assert player.position_ms == 20_000
    output.fired.clear()
    player.pause(False)
    assert output.fired.wait(1)
    player.stop()
    assert output.calls == [(0,), (2,)]


def test_sustain_at_seek_uses_remaining_time_without_retriggering_boundaries():
    score = Score(notes=[Note(0, 0, 3000), Note(1, 1000, 500), Note(2, 1500, 1000)])
    timeline = Timeline(score)
    timeline.seek(1500)
    assert [(n.key, n.duration_ms) for n in timeline.sustaining()] == [(0, 1500)]
    assert [[n.key for n in group] for group in timeline.advance(0, 1)] == [[2]]
    assert score.notes[0].duration_ms == 3000
    timeline.seek(3000)
    assert timeline.sustaining() == []


def test_seek_does_not_resurrect_a_superseded_same_key_articulation():
    timeline = Timeline(Score(notes=[Note(0, 0, 3000), Note(0, 1000, 300)]))
    timeline.seek(1500)
    assert timeline.sustaining() == []


def test_start_mid_note_restores_with_playback_speed():
    output, player = Output(), Player()
    player.load(Score(notes=[Note(3, 0, 3000), Note(4, 4000)]))
    player.seek(1500)
    player.set_speed(2)
    try:
        player.play(output)
        assert output.restored.wait(1)
        assert output.restores == [((3,), (750,))]
        assert output.calls == []
    finally:
        player.stop()


@pytest.mark.parametrize('paused', [False, True])
def test_live_or_paused_seek_restores_once_after_silence(paused):
    output, player = Output(), Player()
    player.load(Score(notes=[Note(0, 0, 5000), Note(1, 6000)]))
    try:
        player.play(output)
        assert output.fired.wait(1)
        if paused:
            player.pause()
        player.seek(2000)
        if paused:
            assert not output.restored.wait(.03)
            player.pause(False)
        assert output.restored.wait(1)
        assert output.restores == [((0,), (3000,))]
        assert output.silenced >= 1
    finally:
        player.stop()


def test_resume_without_seek_restores_only_remaining_sustain():
    output, player = Output(), Player()
    player.load(Score(notes=[Note(0, 0, 5000)]))
    try:
        player.play(output)
        assert output.fired.wait(1)
        time.sleep(.02)
        player.pause()
        position = player.position_ms
        assert 0 < position < 5000
        player.pause(False)
        assert output.restored.wait(1)
        assert output.restores == [((0,), (5000 - position,))]
    finally:
        player.stop()
