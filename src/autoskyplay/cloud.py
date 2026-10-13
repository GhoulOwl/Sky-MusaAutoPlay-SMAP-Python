"""SMAP cloud protocol and release checking, called from Qt background jobs."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from packaging.version import InvalidVersion, Version

from . import __version__
from .score import parse_score


class CloudClient:
    def __init__(self, base_url: str = "https://musetreehouse.com"):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            timeout=20, follow_redirects=True, headers={"User-Agent": f"AutoSkyPlay/{__version__}"}
        )
        self.user = {}

    def close(self):
        self.client.close()

    def _url(self, path: str) -> str:
        url = urljoin(self.base_url + "/", path)
        if urlparse(url).scheme not in ("https", "http"):
            raise ValueError("云端链接必须为 HTTP(S)")
        return url

    def _json(self, response):
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("云端响应格式错误")
        return data

    def login(self, username: str, password: str):
        if urlparse(self.base_url).scheme != "https":
            raise ValueError("登录需要 HTTPS 连接，请在设置中修改云端地址")
        data = self._json(
            self.client.post(
                self._url("/api/game_login.php"), json={"username": username, "password": password}
            )
        )
        if not data.get("success"):
            raise ValueError(data.get("error", "登录失败"))
        user = data.get("user", {})
        if not user.get("mid"):
            raise ValueError("服务器未返回登录令牌")
        self.user = user
        return user

    def avatar(self):
        filename = Path(str(self.user.get("avatar") or "default.png")).name
        response = self.client.get(self._url("/api/avatar.php"), params={"f": filename})
        response.raise_for_status()
        if len(response.content) > 2 * 1024 * 1024:
            raise ValueError("头像超过 2 MB")
        return response.content

    def list(self, query: str = "", sort: str = "newest", difficulty: int = 0, page: int = 1):
        params = {"q": query, "sort": sort, "per_page": 30, "page": page}
        if difficulty:
            params["difficulty"] = difficulty
        data = self._json(self.client.get(self._url("/api/sheets/list.php"), params=params))
        if data.get("status") != "ok":
            raise ValueError(data.get("msg", "曲库加载失败"))
        return data

    def download(self, item: dict):
        url = self._url(item.get("download_url", ""))
        with self.client.stream("GET", url) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 20 * 1024 * 1024:
                    raise ValueError("下载曲谱超过 20 MB")
                chunks.append(chunk)
        score = parse_score(b"".join(chunks), item.get("title", "未命名"))
        if item.get("cover_url") and not score.root.get("cover"):
            try:
                with self.client.stream("GET", self._url(item["cover_url"])) as response:
                    response.raise_for_status()
                    image = bytearray()
                    for chunk in response.iter_bytes():
                        image.extend(chunk)
                        if len(image) > 2 * 1024 * 1024:
                            raise ValueError("封面超过 2 MB")
                    score.root["cover"] = base64.b64encode(image).decode("ascii")
            except (httpx.HTTPError, ValueError):
                pass  # Score download remains useful when the optional cover is unavailable.
        return score

    def upload(self, score, difficulty: int = 1, tags: str = "", description: str = ""):
        if not self.user.get("mid"):
            raise ValueError("请先登录")
        if urlparse(self.base_url).scheme != "https":
            raise ValueError("上传需要 HTTPS 连接")
        fields = {
            "user_id": str(self.user["id"]),
            "mid": self.user["mid"],
            "title": score.name,
            "artist": score.author,
            "transcribed_by": score.transcriber,
            "difficulty": str(difficulty),
            "tags": tags,
            "description": description,
        }
        content = json.dumps(score.to_data(), ensure_ascii=False).encode("utf-8")
        files = {"file": ("sheet.txt", content, "text/plain")}
        cover = score.root.get("cover", "")
        if cover:
            if cover.startswith("data:"):
                cover = cover.split(",", 1)[-1]
            files["cover"] = ("cover.jpg", base64.b64decode(cover), "image/jpeg")
        data = self._json(self.client.post(self._url("/api/sheets/upload.php"), data=fields, files=files))
        if data.get("status") != "ok":
            raise ValueError(data.get("msg", "上传失败"))
        return data

    def upload_log(self, path: Path):
        if urlparse(self.base_url).scheme != "https":
            raise ValueError("上传日志需要 HTTPS 连接")
        data = self._json(
            self.client.post(
                self._url("/api/game_log.php"),
                json={
                    "mid": self.user.get("mid", ""),
                    "username": self.user.get("username", ""),
                    "version": __version__,
                    "log": path.read_text(encoding="utf-8")[-200_000:],
                },
            )
        )
        if data.get("status") != "ok":
            raise ValueError(data.get("msg", "上传日志失败"))
        return data


def check_release(repo: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("请先设置 Python 版的更新仓库（owner/repo）")
    with httpx.Client(timeout=10, headers={"User-Agent": f"AutoSkyPlay/{__version__}"}) as client:
        response = client.get(f"https://api.github.com/repos/{repo}/releases")
        response.raise_for_status()
        releases = response.json()
    candidates = []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        try:
            version = Version(release.get("tag_name", "").lstrip("vV"))
        except InvalidVersion:
            continue
        if version > Version(__version__):
            candidates.append((version, release))
    return max(candidates, key=lambda x: x[0])[1] if candidates else None
