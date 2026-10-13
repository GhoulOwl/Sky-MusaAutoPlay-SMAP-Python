"""Create deterministic UI review images without audio or global input."""

import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from autoskyplay.audio import ASSETS
from autoskyplay.score import load_score
from autoskyplay.storage import Store
from autoskyplay.ui import style
from autoskyplay.ui.dialogs import SettingsDialog
from autoskyplay.ui.editor import EditorDialog
from autoskyplay.ui.main import MainWindow
from autoskyplay.ui.practice import PracticeDialog

output = Path(__file__).resolve().parents[1] / "artifacts"
output.mkdir(exist_ok=True)
app = QApplication([])
with tempfile.TemporaryDirectory(prefix="autoskyplay-ui-") as temporary:
    store = Store(Path(temporary))
    score = store.add_score(load_score(ASSETS / "exercise.json"))
    window = MainWindow(store, no_audio=True, no_global=True)
    window.add_queue(score)
    window.select_score(score)
    window.favorite(score)
    window.show()
    for theme in style.THEMES:
        style.apply_theme(app, theme)
        app.processEvents()
        window.grab().save(str(output / f"main-{theme}.png"))
        window.queue_button.setChecked(True)
        app.processEvents()
        window.grab().save(str(output / f"main-{theme}-queue.png"))
        window.queue_button.setChecked(False)
        window.volume_button.show_popup()
        app.processEvents()
        window.grab().save(str(output / f"main-{theme}-volume.png"))
        window.volume_button.hide_popup()
    style.apply_theme(app, "dark")
    window.resize(1060, 670)
    app.processEvents()
    window.grab().save(str(output / "main-compact.png"))
    window.queue_button.setChecked(True)
    app.processEvents()
    window.grab().save(str(output / "main-compact-queue.png"))
    window.queue_button.setChecked(False)
    window.volume_button.show_popup()
    app.processEvents()
    window.grab().save(str(output / "main-compact-volume.png"))
    window.volume_button.hide_popup()
    window.resize(1280, 820)
    window.pitch.showPopup()
    app.processEvents()
    window.pitch.view().window().grab().save(str(output / "pitch-options.png"))
    window.pitch.hidePopup()
    editor = EditorDialog(window, score)
    editor.show()
    app.processEvents()
    editor.grab().save(str(output / "editor.png"))
    editor.close()
    practice = PracticeDialog(window, score)
    practice.show()
    app.processEvents()
    practice.grab().save(str(output / "practice.png"))
    practice.close()
    settings = SettingsDialog(window)
    settings.show()
    app.processEvents()
    settings.grab().save(str(output / "settings.png"))
    settings.close()
    window.close()
print("UI captures:", output)
