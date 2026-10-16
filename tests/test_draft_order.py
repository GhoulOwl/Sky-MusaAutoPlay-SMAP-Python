import json
from types import SimpleNamespace

from autoskyplay.netease import NeteaseSong
from autoskyplay.score import Score
from autoskyplay.transcription.drafts import DraftStore, creation_time
from autoskyplay.transcription.events import EventResult


def test_first_generated_order_survives_edits_rename_reopen_and_new_version(tmp_path, monkeypatch):
    clock = [100.]
    monkeypatch.setattr('autoskyplay.transcription.drafts.time.time', lambda: clock[0])
    store = DraftStore(tmp_path)
    song = NeteaseSong('1', 'Song', '', '', '')
    result = EventResult([], 1000, 120, {}, {})
    score = Score('Song')

    def save(ident=None):
        return store.save(ident, song, result, score, {})

    first = save()
    clock[0] = 200
    second = save()
    clock[0] = 300
    save(first)
    store.rename(first, 'Edited name')
    data, *_ = store.load(first)
    assert data['created'] == 100 and data['updated'] == 300
    assert [e['id'] for e in store.entries()] == [second, first]
    clock[0] = 400
    accepted_version = save()
    reopened = DraftStore(tmp_path)
    assert [e['id'] for e in reopened.entries()] == [accepted_version, second, first]


def test_legacy_migration_freezes_inferred_creation_and_preserves_audio_lru(tmp_path, monkeypatch):
    store = DraftStore(tmp_path)
    ident = '1'*32
    directory = store.path(ident)
    directory.mkdir()
    path = directory/'draft.json'
    path.write_text(json.dumps(dict(id=ident, schema=1, updated=100., title='Old')))
    mtime = path.stat().st_mtime_ns
    first = store.entries()[0]
    assert first['created'] == 100. and first['created_estimated']
    assert path.stat().st_mtime_ns == mtime
    monkeypatch.setattr('autoskyplay.transcription.drafts.time.time', lambda: 500.)
    store.rename(ident, 'Renamed')
    second = store.entries()[0]
    assert second['created'] == 100. and second['updated'] == 500.
    assert json.loads(path.read_text())['created'] == 100.


def test_legacy_birth_time_used_when_available_never_mutable_ctime():
    for stat, expected in [(SimpleNamespace(st_birthtime=50, st_ctime=500), 50),
                           (SimpleNamespace(st_birthtime=200, st_ctime=500), 100),
                           (SimpleNamespace(st_ctime=20), 100)]:
        data = dict(updated=100)
        assert creation_time(data, SimpleNamespace(stat=lambda: stat)) == expected
        data['updated'] = 600
        assert creation_time(data, SimpleNamespace(stat=lambda: stat)) == expected
