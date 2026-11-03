from PySide6.QtCore import QPoint, Qt

from autoskyplay.score import Score
from autoskyplay.ui.editor import PianoRoll


def test_editor_playhead_centers_and_scrolls_to_end(qtbot):
    roll = PianoRoll(Score())
    qtbot.addWidget(roll)
    roll.resize(600, 350)
    roll.show()
    qtbot.waitExposed(roll)

    width = roll.viewport().width()
    roll.set_cursor(1)
    roll.follow_cursor()
    assert roll.horizontalScrollBar().value() == 0

    roll.set_cursor(10)
    roll.follow_cursor()
    assert abs(roll.mapFromScene(10 * roll.pixels_per_beat, 0).x() - width / 2) <= 1

    roll.set_cursor(roll.total_beats)
    roll.follow_cursor()
    end_x = roll.total_beats * roll.pixels_per_beat
    assert abs(roll.mapFromScene(end_x, 0).x() - width / 2) <= 1

    # The extra space used for centering must not create notes past the score.
    qtbot.mouseClick(roll.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(width - 10, 30))
    assert not roll.score.notes

    roll.resize(800, 350)
    roll.follow_cursor()
    assert abs(roll.mapFromScene(end_x, 0).x() - roll.viewport().width() / 2) <= 1

    roll.set_cursor(0)
    roll.follow_cursor()
    assert roll.horizontalScrollBar().value() == 0
