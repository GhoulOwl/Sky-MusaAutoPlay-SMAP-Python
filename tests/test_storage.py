import json

from autoskyplay.score import Note, Score
from autoskyplay.storage import Store, scan_library


def test_import_collection_and_recoverable_removal(tmp_path):
    source = tmp_path / "source.json"
    Score("Song", notes=[Note(0, 0)]).save(source)
    store = Store(tmp_path / "data")
    imported = store.import_score(source)
    store.toggle_favorite(imported.path)
    assert Store(store.directory).is_favorite(imported.path)
    store.remove(imported.path)
    assert source.exists()
    assert not imported.path.exists()
    assert list((store.directory / "trash").iterdir())
    assert not scan_library(store.songs_dir).scores


def test_corrupt_settings_are_preserved_and_reset(tmp_path):
    state = {"theme": "unknown", "keys": None, "collections": [None]}
    (tmp_path / "state.json").write_text(json.dumps(state))
    store = Store(tmp_path)
    assert store.data["theme"] == "dark"
    assert len(store.data["keys"]) == 15
    backups = list(tmp_path.glob("state.broken*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == state


def test_existing_playback_order_is_preserved(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"loop": "list"}))
    store = Store(tmp_path)
    assert store.data["loop"] == "list"
    store.data["loop"] = "sequence"
    store.save()
    assert Store(tmp_path).data["loop"] == "sequence"
