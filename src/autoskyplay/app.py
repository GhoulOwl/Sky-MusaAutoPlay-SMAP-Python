from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .audio import ASSETS
from .score import load_score
from .storage import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description="AutoSkyPlay — Python Sky music studio")
    parser.add_argument("--data-dir", type=Path, help="Override user data directory")
    parser.add_argument("--no-audio", action="store_true", help="Run without opening an audio device")
    parser.add_argument("--no-global-hotkeys", action="store_true")
    parser.add_argument("--smoke-test", action="store_true", help="Open UI, capture if requested, then exit")
    parser.add_argument("--transcribe", action="store_true", help="Open the local transcription window")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--import", dest="imports", type=Path, nargs="*", default=[])
    parser.add_argument("--inspect", type=Path, help="Print score metadata without starting the UI")
    args = parser.parse_args(argv)
    if args.inspect:
        score = load_score(args.inspect)
        print(
            json.dumps(
                {
                    "name": score.name,
                    "bpm": score.bpm,
                    "notes": len(score.notes),
                    "duration_ms": score.duration_ms,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.smoke_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from .ui.main import MainWindow

    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("AutoSkyPlay")
    app.setApplicationVersion("0.1.0")
    store = Store(args.data_dir)
    handler = RotatingFileHandler(
        store.directory / "autoskyplay.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    # The bundled exercise is original, not copied from the third-party song library.
    if not store.data.get("initialized"):
        if not list(store.songs_dir.iterdir()):
            store.import_score(ASSETS / "exercise.json")
        store.data["initialized"] = True
        store.save()
    for path in args.imports:
        store.import_score(path)
    window = MainWindow(
        store, no_audio=args.no_audio or args.smoke_test, no_global=args.no_global_hotkeys or args.smoke_test,
        restore_netease=not args.smoke_test,
    )
    window.show()
    if args.transcribe:
        window.transcribe()
    if args.screenshot:

        def capture():
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            target = window.transcription_dialog if args.transcribe else window
            target.grab().save(str(args.screenshot))

        QTimer.singleShot(350, capture)
    if args.smoke_test:
        QTimer.singleShot(550, window.close)
        QTimer.singleShot(650, app.quit)
    code = app.exec()
    window.player.stop()
    window.pool.waitForDone()
    if window.transcription_dialog and window.transcription_dialog.task:
        window.transcription_dialog.task.thread.join(timeout=25)
    window.cloud_client.close()
    window.netease_client.close()
    window.netease_service.close()
    handler.close()
    logging.getLogger().removeHandler(handler)
    return code
