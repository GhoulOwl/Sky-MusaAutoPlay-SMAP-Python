import hashlib
import json
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from autoskyplay.midi import MidiImporter
from autoskyplay.netease import NeteaseAudio, NeteaseClient, NeteaseError, NeteaseSong
from autoskyplay.score import load_score
from autoskyplay.transcription.engine import Engine, find_engine, install_engine, platform_tag
from autoskyplay.transcription.melody import PitchResult, export_midi, to_score
from autoskyplay.transcription.task import TranscriptionTask


def contour(pitches, confidence=None, onset=None):
    pitch = np.asarray(pitches, dtype=float)
    return PitchResult(440 * 2 ** ((pitch - 69) / 12),
                       np.full(len(pitch), .9) if confidence is None else np.asarray(confidence),
                       np.full(len(pitch), -20.), np.zeros(len(pitch)) if onset is None else np.asarray(onset),
                       duration_ms=len(pitch) * 10)


def test_vibrato_silence_and_same_pitch_attacks():
    pitches = 60 + .2 * np.sin(np.arange(100) / 2)
    confidence = np.full(100, .9)
    confidence[40:50] = 0
    onset = np.zeros(100)
    onset[75] = 1
    notes = contour(pitches, confidence, onset).notes()
    assert [(n.pitch, n.start_ms, n.duration_ms) for n in notes] == [
        (60, 0, 400), (60, 500, 250), (60, 750, 250)]


def test_filtering_does_not_change_timing_or_destroy_raw_result(tmp_path):
    result = contour([60] * 40 + [61] * 30 + [84] * 30)
    raw = result.frequency.copy()
    notes = result.notes()
    score, stats = to_score(notes, "Melody", bpm=95.3, shift=1,
                            metadata={"source_start": 60, "is_trial": True})
    assert stats["semitones"] > 0 and stats["folded"] > 0
    assert [n.time_ms for n in score.notes] == [0, 400, 700]
    assert score.notes[1].extra["sourcePitch"] == 61
    np.testing.assert_equal(result.frequency, raw)
    score.save(tmp_path / "result.json")
    restored = load_score(tmp_path / "result.json")
    assert restored.root["transcription"]["source_start"] == 60
    assert restored.notes[1].extra["sourcePitch"] == 61
    export_midi(notes, tmp_path / "raw.mid", bpm=95.3, name="中文旋律")
    importer = MidiImporter(tmp_path / "raw.mid")
    assert importer.tracks()[0][1] == "中文旋律"
    imported = importer.convert(shift=0)
    assert [n.time_ms for n in imported.notes] == pytest.approx([0, 400, 700], abs=1)


def test_silence_short_fragments_and_grid_collisions():
    result = contour([60] * 20)
    result.energy_db[:] = -100
    assert not result.notes()
    with pytest.raises(ValueError, match="未识别"):
        to_score([], "Silence")
    result = contour([60] * 3 + [64] * 30)
    assert [n.pitch for n in result.notes()] == [64]
    result.confidence[0] = np.nan
    with pytest.raises(ValueError, match="无效"):
        result.notes()


def client(handler):
    return NeteaseClient(SimpleNamespace(ensure_running=lambda: None, token="local-secret",
                                         base_url="http://127.0.0.1:3000"), httpx.MockTransport(handler))


def test_trial_audio_download_is_credential_free_and_atomic(tmp_path):
    def handler(request):
        if request.url.path == "/song/url/v1":
            body = json.loads(request.content)
            assert body["cookie"] == "MUSIC_U=login-secret"
            assert body["id"] == "1" and "unblock" not in body
            return httpx.Response(200, json={"data": [{"id": 1, "code": 200, "time": 180000,
                "url": "http://m1.music.126.net/a.mp3", "freeTrialInfo": {"start": 20, "end": 50}}]})
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert request.url.scheme == "https"
        return httpx.Response(200, content=b"audio", headers={"content-length": "5"})

    remote = client(handler)
    remote.cookie = "MUSIC_U=login-secret"
    remote.media.cookies.set("MUSIC_U", "other-secret", domain="m1.music.126.net")
    try:
        audio = remote.playback_audio("1")
        assert audio.is_trial and audio.available_seconds == 30
        target = tmp_path / "audio"
        remote.download_audio(audio, target)
        assert target.read_bytes() == b"audio" and not (tmp_path / "audio.part").exists()
    finally:
        remote.close()


@pytest.mark.parametrize("value", [
    {"id": 2, "url": "https://m1.music.126.net/a"},
    {"id": 1, "url": "https://localhost/private"},
    {"id": 1, "url": "https://m1.music.126.net/a", "freeTrialInfo": {"start": 30, "end": 20}},
    {"id": 1, "url": None, "code": 404},
])
def test_invalid_or_unavailable_audio(value):
    remote = client(lambda _: httpx.Response(200, json={"data": [value]}))
    try:
        with pytest.raises(NeteaseError):
            remote.playback_audio("1")
    finally:
        remote.close()


@pytest.mark.parametrize("mode", ["redirect", "limit", "cancel", "truncated", "html"])
def test_download_failures_remove_partial_files(tmp_path, mode):
    def handler(_):
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        if mode == "html":
            return httpx.Response(200, content=b"login", headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"123456", headers={"content-length": "10" if mode == "truncated" else "6"})
    remote = client(handler)
    try:
        with pytest.raises(NeteaseError):
            remote.download_audio(NeteaseAudio("1", "https://m1.music.126.net/audio", 30000), tmp_path / "audio",
                                  cancelled=lambda: mode == "cancel", max_bytes=3 if mode == "limit" else 100)
        assert not list(tmp_path.iterdir())
    finally:
        remote.close()


def engine_archive(path, extra=None, tag=None, corrupt=False):
    files = {"engine": b"engine executable", "models/models.json": b"{}"}
    files.update(extra or {})
    manifest = dict(schema=1, version="1.0.0", platform=tag or platform_tag(), entry="engine",
                    files={name: hashlib.sha256(data).hexdigest() for name, data in files.items()})
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("engine.json", json.dumps(manifest))
        for name, data in files.items():
            archive.writestr(name, b"changed" if corrupt and name == "engine" else data)


def test_offline_component_install_is_atomic_and_validated(tmp_path):
    archive = tmp_path / "engine.zip"
    engine_archive(archive)
    engine = install_engine(archive, tmp_path / "data")
    assert engine.command == find_engine(tmp_path / "data").command
    assert Path(engine.command[0]).is_file()
    pointer = (tmp_path / "data/transcription/current.json").read_bytes()
    engine_archive(archive, corrupt=True)
    with pytest.raises(ValueError, match="校验失败"):
        install_engine(archive, tmp_path / "data")
    assert (tmp_path / "data/transcription/current.json").read_bytes() == pointer
    assert not list((tmp_path / "data/transcription/components").glob(".staging-*"))


def test_relative_data_directory_produces_absolute_worker_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archive = tmp_path / "engine.zip"
    engine_archive(archive)
    engine = install_engine(archive, Path("data"))
    assert Path(engine.command[0]).is_absolute() and engine.models.is_absolute()


@pytest.mark.parametrize("extra,tag", [({"../escape": b"bad"}, None),
                                       ({"C:/escape": b"bad"}, None), ({}, "wrong-platform")])
def test_component_paths_and_platform_are_checked(tmp_path, extra, tag):
    archive = tmp_path / "engine.zip"
    engine_archive(archive, extra, tag)
    with pytest.raises(ValueError):
        install_engine(archive, tmp_path / "data")
    assert not (tmp_path / "escape").exists()


def fake_task(tmp_path, worker):
    script = tmp_path / "worker.py"
    script.write_text(worker, encoding="utf-8")
    song = NeteaseSong("1", "Example", "Artist", "Album", "")
    audio = NeteaseAudio("1", "https://m1.music.126.net/a", 30000, 10, 40)

    def download(audio, path, cancelled, progress):
        path.write_bytes(b"audio")
        progress(100)

    source = SimpleNamespace(download_audio=download, cover=lambda song: b"", playback_audio=lambda song: audio)
    return TranscriptionTask(tmp_path, source, song, audio, Engine([sys.executable, str(script)], tmp_path), 15, 10)


def test_worker_result_preserves_source_offset_and_cleanup(tmp_path):
    task = fake_task(tmp_path, '''
import argparse, json, pathlib, numpy as np
p=argparse.ArgumentParser();p.add_argument('--work');p.add_argument('--models');p.add_argument('--parent');a=p.parse_args()
w=pathlib.Path(a.work)
assert json.loads((w/'request.json').read_text()) == {'start':5,'length':10}
np.savez(w/'pitch.npz',frequency=np.full(100,440),confidence=np.ones(100),energy_db=np.full(100,-20),onset=np.zeros(100),hop_ms=10,duration_ms=1000,bpm=120)
print(json.dumps({'stage':'done','engine':'1.0.0'}),flush=True)
''')
    task.start()
    task.thread.join(timeout=10)
    assert not task.running and task.result is not None
    assert task.result.metadata["source_start"] == 15 and task.result.metadata["is_trial"]
    assert task.result.notes()[0].start_ms == 0
    task.close()
    assert not task.work.exists()


def test_cancel_stops_worker_and_cleans_task(tmp_path):
    task = fake_task(tmp_path, "import time;time.sleep(60)")
    task.start()
    deadline = time.monotonic() + 5
    while task.process is None and task.running and time.monotonic() < deadline:
        time.sleep(.01)
    assert task.process is not None
    task.close()
    task.thread.join(timeout=8)
    assert not task.running and task.process.poll() is not None and not task.work.exists()


def test_worker_crash_does_not_generate_a_score(tmp_path):
    task = fake_task(tmp_path, "raise RuntimeError('crash')")
    task.start()
    task.thread.join(timeout=8)
    assert task.result is None
    events = []
    while not task.events.empty():
        events.append(task.events.get())
    assert any(e["stage"] == "error" and "异常退出" in e["message"] for e in events)
    task.close()


def test_worker_protocol_uses_utf8_despite_inherited_locale(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    task = fake_task(tmp_path, "import json,sys;print(json.dumps({'stage':'error','message':'模型文件缺失'},ensure_ascii=False),flush=True);sys.exit(1)")
    task.start()
    task.thread.join(timeout=8)
    events = []
    while not task.events.empty():
        events.append(task.events.get())
    assert any(e.get("message") == "模型文件缺失" for e in events)
    task.close()


def test_close_at_finished_callback_still_removes_temporary_directory(tmp_path):
    task = fake_task(tmp_path, "raise RuntimeError('crash')")
    original = task.emit

    def emit(stage, *args, **kwargs):
        if stage == "finished":
            task.close()
        original(stage, *args, **kwargs)

    task.emit = emit
    task.start()
    task.thread.join(timeout=8)
    assert not task.running and not task.work.exists()
