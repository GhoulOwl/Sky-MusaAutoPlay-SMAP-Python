import json

import httpx
import numpy as np
import pytest

from autoskyplay.netease import NeteaseAudio, NeteaseClient, NeteaseError, NeteaseSong
from autoskyplay.transcription.cache import DownloadCache
from autoskyplay.transcription.drafts import DraftStore
from autoskyplay.transcription.lyrics import lyric_line, parse_lrc, parse_yrc
from autoskyplay.transcription.melody import PitchResult


def test_lyric_parsers_and_timeline():
    lines = parse_yrc('[61000,2200](61000,500,0)你(61500,900,0)好\n[65000,1000](65000,300,0)<呀>')
    assert lines[0]['text'] == '你好' and lines[0]['words'][1]['start'] == 61500
    assert lyric_line({'lines': lines}, 60999) is None
    assert lyric_line({'lines': lines}, 62000)['text'] == '你好'
    assert lyric_line({'lines': lines}, 65000)['text'] == '<呀>'
    lines = parse_lrc('[offset:-100]\n[01:01.5][01:03.05]同一句\n[ar:歌手]')
    assert [line['start'] for line in lines] == [61400, 62950]
    assert all(line['text'] == '同一句' for line in lines)


def client_with(handler):
    class Service:
        base_url = 'http://localhost'
        token = 'token'
        def ensure_running(self):
            pass
    return NeteaseClient(Service(), transport=httpx.MockTransport(handler))


def test_request_prefers_yrc_and_falls_back():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert json.loads(request.content)['id'] == '1'
        return httpx.Response(200, json={'code': 200, 'yrc': {'lyric': '[1000,500](1000,500,0)字'}})
    client = client_with(handler)
    assert client.lyrics('1')['format'] == 'yrc'
    assert calls == ['/lyric/new']
    client.close()

    def legacy(request):
        if request.url.path == '/lyric/new':
            return httpx.Response(502)
        return httpx.Response(200, json={'code': 200, 'lrc': {'lyric': '[00:01.00]一行'}})
    client = client_with(legacy)
    assert client.lyrics('1')['lines'][0]['text'] == '一行'
    client.close()


def test_no_lyrics_and_network_failure():
    client = client_with(lambda request: httpx.Response(200, json={'code': 200, 'nolyric': True}))
    assert client.lyrics('1')['format'] == 'none'
    client.close()
    client = client_with(lambda request: httpx.Response(502))
    with pytest.raises(NeteaseError, match='歌词获取失败'):
        client.lyrics('1')
    client.close()


def test_cache_and_draft_keep_lyrics(tmp_path):
    lyrics = {'song_id': '1', 'format': 'lrc', 'lines': parse_lrc('[01:00.00]歌词')}
    audio = NeteaseAudio('1', '', 90000, 60, 90)
    source = tmp_path / 'audio'
    source.write_bytes(b'audio')
    cache = DownloadCache(tmp_path)
    cache.put(audio, source)
    cache.put_lyrics('1', lyrics)
    cache.put(audio, source)
    assert cache.get_lyrics('1') == lyrics
    store = DraftStore(tmp_path)
    result = PitchResult(np.ones(10), np.ones(10), np.ones(10), np.ones(10),
                         duration_ms=1000, metadata={'source': 'netease', 'source_start': 60})
    ident = store.save(None, NeteaseSong('1', '歌', '', '', ''), result, None, {}, lyrics=lyrics)
    assert store.load(ident)[0]['lyrics'] == lyrics
