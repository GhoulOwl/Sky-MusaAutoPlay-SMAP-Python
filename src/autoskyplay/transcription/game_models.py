"""Fixed GAME 1.0 medium weights from the authors' GitHub release."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import httpx

from .models import ModelStore, check_cancelled, digest

GAME_REVISION = "f4239345f0fed21890d362c74a35fd857724aa82"
GAME_ARCHIVE_SHA = "8c5b3e531e2905b935e664e2f533921cd637243770fab5282413bdb5051ca60c"
GAME_ARCHIVE_SIZE = 184550485
GAME_MODELS = {
    "medium": dict(
        revision="1.0",
        files={
            "model.pt": (199764599, "e9904159fb0646e1a352b9d2bc74615547cfa3e32d45c7464d440ac142846d93"),
            "config.yaml": (1690, "c4cca41410bfce49859db5544e1a0820749f9795d4e3f97e5283917ceeeea813"),
            "lang_map.json": (37, "cf7ca24d93682464c5d57d1cd7d238d29d565f7e5774575dbcdff7034852ca37"),
        },
    )
}


class GameModelStore(ModelStore):
    catalog = GAME_MODELS
    prefix = "game"
    title = "GAME"
    weight_name = "model.pt"
    license = "CC-BY-NC-SA-4.0"

    def repository(self, size):
        return "openvpi/GAME/releases/tag/v1.0.0"

    def download_lock(self, size):
        downloader = ModelStore.__new__(ModelStore)
        downloader.root, downloader.catalog, downloader.prefix = self.root, self.catalog, "game-download"
        return downloader.lock(size)

    def delete(self, size="medium"):
        with self.download_lock(size):
            super().delete(size)
            shutil.rmtree(self.root / ".game-medium-download", ignore_errors=True)

    def install(
        self, size="medium", *, source=None, cancelled=lambda: False, progress=lambda p: None, client=None
    ):
        if source is not None or self.installed(size):
            return super().install(size, source=source, cancelled=cancelled, progress=progress, client=client)
        # The download lock has its own namespace. The normal model lock still
        # protects install/verify/delete and inference against partial publication.
        with self.download_lock(size):
            stage = self.root / ".game-medium-download"
            stage.mkdir(exist_ok=True)
            archive = stage / "release.zip"
            length = archive.stat().st_size if archive.exists() else 0
            if (
                shutil.disk_usage(stage).free
                < max(0, GAME_ARCHIVE_SIZE - length)
                + 2 * sum(n for n, _ in self.catalog[size]["files"].values())
                + 64 * 1024**2
            ):
                raise ValueError("磁盘空间不足，无法下载和校验 GAME 模型")
            if length > GAME_ARCHIVE_SIZE:
                archive.unlink()
                length = 0
            owned = client is None
            client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, read=90))
            try:
                if length < GAME_ARCHIVE_SIZE:
                    url = "https://github.com/openvpi/GAME/releases/download/v1.0.0/GAME-1.0-medium.zip"
                    headers = {"Accept-Encoding": "identity"}
                    if length:
                        headers["Range"] = f"bytes={length}-"
                    with client.stream("GET", url, headers=headers) as response:
                        response.raise_for_status()
                        if response.status_code == 200:
                            length = 0
                        elif response.status_code != 206 or response.headers.get("content-range") != (
                            f"bytes {length}-{GAME_ARCHIVE_SIZE - 1}/{GAME_ARCHIVE_SIZE}"
                        ):
                            raise ValueError("GAME 续传范围不匹配")
                        with archive.open("ab" if length else "wb") as output:
                            for chunk in response.iter_bytes(1024**2):
                                check_cancelled(cancelled)
                                length += len(chunk)
                                if length > GAME_ARCHIVE_SIZE:
                                    raise ValueError("GAME 下载大小不符")
                                output.write(chunk)
                                progress(int(90 * length / GAME_ARCHIVE_SIZE))
                if length != GAME_ARCHIVE_SIZE:
                    raise ValueError("GAME 下载未完成，可继续下载")
                if digest(archive, cancelled) != GAME_ARCHIVE_SHA:
                    archive.unlink()
                    raise ValueError("GAME 发布包 SHA256 校验失败")
                with zipfile.ZipFile(archive) as package:
                    # Extract only the three pinned files, never arbitrary ZIP paths.
                    for name, (size_bytes, _) in self.catalog[size]["files"].items():
                        check_cancelled(cancelled)
                        member = f"GAME-1.0-medium/{name}"
                        if package.getinfo(member).file_size != size_bytes:
                            raise ValueError("GAME 发布包文件大小不符")
                        (stage / name).write_bytes(package.read(member))
                installed = super().install(
                    size,
                    source=Path(stage),
                    cancelled=cancelled,
                    progress=lambda p: progress(90 + p // 10),
                    client=client,
                )
                shutil.rmtree(stage, ignore_errors=True)
                return installed
            finally:
                if owned:
                    client.close()
