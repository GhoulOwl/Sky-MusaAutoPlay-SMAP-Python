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
