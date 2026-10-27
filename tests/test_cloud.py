import json

import httpx
import pytest

from autoskyplay.cloud import CloudClient


def test_search_and_download_protocol():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("list.php"):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "total": 1,
                    "pages": 1,
                    "items": [{"title": "Test", "download_url": "/score.txt"}],
                },
            )
        return httpx.Response(
            200, content=json.dumps([{"songNotes": [{"key": "Key4", "time": 100}]}]).encode()
        )

    cloud = CloudClient("https://example.org")
    cloud.client.close()
    cloud.client = httpx.Client(transport=httpx.MockTransport(handler))
    data = cloud.list("曲", "downloads", 3, 2)
    score = cloud.download(data["items"][0])
    assert requests[0].url.params["q"] == "曲"
    assert requests[0].url.params["sort"] == "downloads"
    assert requests[0].url.params["difficulty"] == "3"
    assert score.notes[0].key == 4
    assert score.notes[0].time_ms == 100
    cloud.close()


def test_login_rejects_http_before_transmitting_password():
    cloud = CloudClient("http://example.org")
    with pytest.raises(ValueError, match="HTTPS"):
        cloud.login("user", "secret")
    cloud.close()


def test_bad_download_never_becomes_a_local_score():
    cloud = CloudClient("https://example.org")
    cloud.client.close()
    cloud.client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>Error</html>"))
    )
    with pytest.raises(ValueError):
        cloud.download({"download_url": "/failed.txt"})
    cloud.close()


def test_cover_supports_relative_urls_without_downloading_a_score():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"cover image")

    cloud = CloudClient("https://example.org")
    cloud.client.close()
    cloud.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert cloud.cover({}) == b""
        assert not requests
        assert cloud.cover({"cover_url": "/covers/song.jpg"}) == b"cover image"
        assert len(requests) == 1
        assert str(requests[0].url) == "https://example.org/covers/song.jpg"
    finally:
        cloud.close()


@pytest.mark.parametrize("status,content,error", [
    (404, b"", httpx.HTTPStatusError),
    (200, b"x" * (2 * 1024 * 1024 + 1), ValueError),
])
def test_cover_rejects_failed_or_oversized_images(status, content, error):
    cloud = CloudClient("https://example.org")
    cloud.client.close()
    cloud.client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, content=content))
    )
    try:
        with pytest.raises(error):
            cloud.cover({"cover_url": "/cover.jpg"})
    finally:
        cloud.close()
