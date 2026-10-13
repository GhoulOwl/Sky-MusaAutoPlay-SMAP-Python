"""Download only assets from the configured Python application's releases."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .score import safe_name


def installer_asset(release: dict) -> dict | None:
    suffix = ".exe" if sys.platform == "win32" else ".dmg" if sys.platform == "darwin" else ".AppImage"
    return next(
        (
            a
            for a in release.get("assets", [])
            if a.get("name", "").startswith("AutoSkyPlay") and a["name"].endswith(suffix)
        ),
        None,
    )


def download_asset(
    asset: dict, directory: Path, on_progress=lambda value: None, cancelled=lambda: False
) -> Path:
    url = asset["browser_download_url"]
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("更新文件须来自配置仓库的 GitHub Release")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / safe_name(asset["name"])
    partial = target.with_suffix(target.suffix + ".download")
    received, started = 0, time.monotonic()
    expected = int(asset.get("size", 0))
    digest = hashlib.sha256()
    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with partial.open("wb") as file:
                    for chunk in response.iter_bytes():
                        if cancelled():
                            raise RuntimeError("更新下载已取消")
                        if time.monotonic() - started > 600:
                            raise TimeoutError("更新下载超过 10 分钟")
                        received += len(chunk)
                        if received > (expected if expected else 1024**3):
                            raise ValueError("下载文件大小与发布信息不符")
                        file.write(chunk)
                        digest.update(chunk)
                        on_progress(int(received / max(1, expected) * 100) if expected else 0)
        if expected and received != expected:
            raise ValueError("更新文件下载不完整")
        declared = asset.get("digest", "")
        if declared and declared != "sha256:" + digest.hexdigest():
            raise ValueError("更新文件校验失败")
        os.replace(partial, target)
        return target
    finally:
        partial.unlink(missing_ok=True)
