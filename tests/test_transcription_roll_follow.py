from autoskyplay.score import Score
from autoskyplay.ui.transcription_widgets import PreviewRoll


def test_preview_playhead_follows_center_and_preserves_manual_view(qtbot):
    roll = PreviewRoll()
    qtbot.addWidget(roll)
    roll.resize(600, 350)
    roll.show()
    roll.set_score(Score(), 30000)
    qtbot.waitExposed(roll)

    beat_ms = roll.score.ms_per_beat
    width = roll.viewport().width()
    roll.set_position(beat_ms)
    assert roll.horizontalScrollBar().value() == 0

    roll.set_position(beat_ms * 10)
    assert abs(roll.mapFromScene(10 * roll.pixels_per_beat, 0).x() - width / 2) <= 1

    roll.set_position(30000)
    end_x = 30000 / beat_ms * roll.pixels_per_beat
    assert abs(roll.mapFromScene(end_x, 0).x() - width / 2) <= 1

    roll.follow = False
    roll.horizontalScrollBar().setValue(25)
    roll.set_position(beat_ms * 10)
    assert roll.horizontalScrollBar().value() == 25

    roll.follow = True
    roll.set_position(0)
    assert roll.horizontalScrollBar().value() == 0

    roll.fit()
    roll.set_position(30000)
    assert roll.horizontalScrollBar().value() == 0
