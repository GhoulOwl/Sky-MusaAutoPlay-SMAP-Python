import io
import sys
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
import soundfile as sf

from autoskyplay.audio_files import compress_audio
from autoskyplay.netease import NeteaseAudio, NeteaseClient, NeteaseSong
from autoskyplay.transcription.cache import DownloadCache
from autoskyplay.transcription.source import AudioSource


def recording():
    time = np.arange(96000) / 48000
    samples = .4 * np.column_stack([np.sin(2 * np.pi * 440 * time), np.sin(2 * np.pi * 660 * time)])
    samples[:12000] = 0
    samples[-12000:] = 0
    result = io.BytesIO()
    sf.write(result, samples, 48000, format="WAV", subtype="FLOAT")
    return result.getvalue()


@pytest.mark.parametrize("limit,channels", [(64 * 1024, 2), (12 * 1024, 1)])
def test_compression_preserves_complete_duration_and_original_file(tmp_path, limit, channels):
    source, target = tmp_path / "原始录音.wav", tmp_path / "source.audio"
    original = recording()
    source.write_bytes(original)
    progress = []
    compress_audio(source, target, progress=progress.append, max_bytes=limit)
    assert target.stat().st_size <= limit
    assert source.read_bytes() == original
    info = sf.info(target)
    assert info.duration == pytest.approx(2, abs=.01)
    assert info.channels == channels
    samples, rate = sf.read(target, always_2d=True)
    assert np.max(np.abs(samples[int(rate * .4):int(rate * 1.5)])) > .2
    assert np.max(np.abs(samples[:int(rate * .1)])) < .01
    assert np.max(np.abs(samples[-int(rate * .1):])) < .01
    assert progress[-1] == 100 and progress == sorted(progress)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.audio", "原始录音.wav"]


def test_cancelled_compression_keeps_previous_target_and_cleans_temporary_files(tmp_path):
    source, target = tmp_path / "source.wav", tmp_path / "source.audio"
    source.write_bytes(recording())
    target.write_bytes(b"previous audio")
    cancelled = [False]

    def progress(value):
        cancelled[0] = True

    with pytest.raises(ValueError, match="取消"):
        compress_audio(source, target, cancelled=lambda: cancelled[0], progress=progress)
    assert target.read_bytes() == b"previous audio"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.audio", "source.wav"]


def test_failed_compression_keeps_previous_target(tmp_path, monkeypatch):
    source, target = tmp_path / "source.wav", tmp_path / "source.audio"
    source.write_bytes(recording())
    target.write_bytes(b"previous audio")
    # Python rejects FFmpeg arguments and exits; exercise real subprocess failure cleanup.
    monkeypatch.setattr("autoskyplay.audio_files.imageio_ffmpeg.get_ffmpeg_exe", lambda: sys.executable)
    with pytest.raises(ValueError, match="压缩失败"):
        compress_audio(source, target)
    assert target.read_bytes() == b"previous audio"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.audio", "source.wav"]


@pytest.mark.parametrize("content_length", [True, False])
def test_oversized_download_compresses_known_and_unknown_length_streams(tmp_path, content_length):
    original = recording()

    class AudioStream(httpx.SyncByteStream):
        def __iter__(self):
            for offset in range(0, len(original), 4096):
                yield original[offset:offset + 4096]

    def handler(request):
        assert "cookie" not in request.headers and "authorization" not in request.headers
        headers = {"content-length": str(len(original))} if content_length else {}
        return httpx.Response(200, stream=AudioStream(), headers=headers)

    client = NeteaseClient(SimpleNamespace(), transport=httpx.MockTransport(handler))
    target = tmp_path / "source.audio"
    target.write_bytes(b"previous audio")
    progress = []
    try:
        audio = NeteaseAudio("1", "https://m1.music.126.net/song", 2000, 10, 12)
        client.download_audio(audio, target, progress=progress.append, max_bytes=64 * 1024)
        assert target.stat().st_size < 64 * 1024
        assert sf.info(target).duration == pytest.approx(audio.available_seconds, abs=.01)
        assert progress[-1] == 100 and progress == sorted(progress)
        assert list(tmp_path.iterdir()) == [target]
    finally:
        client.close()


def test_oversized_local_audio_compresses_offline_without_touching_original(tmp_path, monkeypatch):
    original = recording()
    path = tmp_path / "原始录音.wav"
    path.write_bytes(original)
    monkeypatch.setattr("autoskyplay.transcription.source.MAX_AUDIO_BYTES", 64 * 1024)
    source = AudioSource(tmp_path, SimpleNamespace(), NeteaseSong("1", "本地录音", "", "", "", 2000),
                         NeteaseAudio("1", "", 2000), local_path=path)
    try:
        waveform = source.prepare()
        assert waveform["kind"] == "local_audio"
        assert waveform["duration"] == pytest.approx(2, abs=.01)
        assert max(waveform["peaks"]) > .2
        assert (source.work / "source.audio").stat().st_size < 64 * 1024
        assert path.read_bytes() == original
        assert not list(DownloadCache(tmp_path).root.glob("*/entry.json"))
    finally:
        source.close()
