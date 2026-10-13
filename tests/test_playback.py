import threading
import time

from autoskyplay.playback import Player, State, Timeline
from autoskyplay.score import Note, Score


class Output:
    def __init__(self):
        self.calls = []
        self.silenced = 0
        self.fired = threading.Event()

    def trigger(self, keys, durations):
        self.calls.append(keys)
        self.fired.set()

    def pump(self):
        pass

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
