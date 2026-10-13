import json

import pytest

from autoskyplay.score import ScoreError, load_score, parse_key, parse_score, safe_name


def sample():
    return [
        {
            "name": "测试",
            "bpm": 123.45,
            "extension": {"tag": "kept"},
            "songNotes": [
                {"key": "1Key0", "time": 0, "velocity": 90},
                {"key": 14, "time": 987.654321, "duration": 250},
            ],
        },
        {"other": "array entry"},
    ]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-be", "utf-16-le"])
def test_encoded_score_roundtrip_preserves_time_and_extensions(tmp_path, encoding):
    raw = json.dumps(sample(), ensure_ascii=False).encode(encoding)
    score = parse_score(raw)
    target = tmp_path / "song.json"
    score.save(target)
    loaded = load_score(target)
    assert loaded.name == "测试"
    assert loaded.notes[1].time_ms == 987.654321
    assert loaded.root["extension"] == {"tag": "kept"}
    assert loaded.notes[0].extra["velocity"] == 90
    assert loaded.tail == [{"other": "array entry"}]


@pytest.mark.parametrize("key", [True, -1, 15, "1Key15", "garbage5", None])
def test_invalid_keys_rejected(key):
    with pytest.raises(ScoreError):
        parse_key(key)


@pytest.mark.parametrize("value", ["Key4", "1Key4", "4", 4])
def test_legacy_key_formats(value):
    assert parse_key(value) == 4


def test_malformed_notes_report_index():
    with pytest.raises(ScoreError, match="第 1 个"):
        parse_score('[{"songNotes":[{"key":"1Key0","time":-1}]}]')


def test_invalid_bpm_reverts_without_retiming():
    data = sample()
    data[0]["bpm"] = 0
    score = parse_score(json.dumps(data))
    assert score.bpm == 120
    assert score.notes[1].time_ms == 987.654321


def test_bpm_change_is_metadata_only_by_default():
    score = parse_score(json.dumps(sample()))
    score.set_bpm(200)
    assert score.notes[1].time_ms == 987.654321


def test_windows_safe_names():
    assert safe_name("../../CON") == "_.._CON"
    assert safe_name("CON") == "_CON"
    assert "/" not in safe_name("bad/name")
