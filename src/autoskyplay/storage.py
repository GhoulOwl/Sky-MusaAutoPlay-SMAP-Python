"""User data, persistent collections and library import, without installation-dir writes."""

from __future__ import annotations

import copy
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

from .input import DEFAULT_KEYS, validate_keys
from .score import Score, atomic_json, load_score, unique_path

log = logging.getLogger(__name__)


class Store:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or user_data_path("AutoSkyPlay", appauthor=False)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.songs_dir = self.directory / "songs"
        self.songs_dir.mkdir(exist_ok=True)
        self.path = self.directory / "state.json"
        self.data = {
            "theme": "dark",
            "language": "zh_CN",
            "instrument": "Piano",
            "pitches": {},
            "speed": 1.0,
            "volume": 0.6,
            "cave": False,
            "countdown": 3,
            "hold_ms": 25,
            "keys": list(DEFAULT_KEYS),
            "collections": [{"name": "我的收藏", "files": []}],
            "queue": [],
            "current": "",
            "position_ms": 0,
            "loop": "sequence",
            "font_size": 13,
            "global_hotkeys": True,
            "random_speed": False,
            "cloud_url": "https://musetreehouse.com",
            "update_repo": "",
            "netease_port": 3000,
            "remember_netease_login": False,
        }
        defaults = copy.deepcopy(self.data)
        if self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("state must be an object")
                self.data.update(value)
                validate_keys(self.data["keys"])
                self.validate()
            except (OSError, ValueError, TypeError) as exc:
                backup = unique_path(self.directory, "state.broken")
                self.path.replace(backup)
                log.warning("损坏的配置已保存在 %s: %s", backup, exc)
                self.data = defaults

    def validate(self):
        from .audio import INSTRUMENTS
        from .netease import validate_port

        validate_port(self.data["netease_port"])

        for field, allowed in {
            "theme": ("dark", "light", "sunset"),
            "language": ("zh_CN", "zh_TW", "en", "ja"),
            "loop": ("sequence", "list", "single", "shuffle"),
            "instrument": INSTRUMENTS,
        }.items():
            if self.data[field] not in allowed:
                raise ValueError(f"无效配置字段: {field}")
        for field, lower, upper in [
            ("speed", 0.1, 3),
            ("volume", 0, 1),
            ("countdown", 0, 30),
            ("hold_ms", 10, 500),
            ("font_size", 10, 22),
            ("position_ms", 0, 86400000),
        ]:
            value = self.data[field]
            if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"无效配置字段: {field}")
        for field in ("cave", "global_hotkeys", "random_speed", "remember_netease_login"):
            if not isinstance(self.data[field], bool):
                raise ValueError(f"无效配置字段: {field}")
        for field in ("queue",):
            if not isinstance(self.data[field], list) or not all(
                isinstance(x, str) for x in self.data[field]
            ):
                raise ValueError(f"无效配置字段: {field}")
        if not isinstance(self.data["pitches"], dict) or not all(
            isinstance(k, str) and type(v) is int and -36 <= v <= 36 for k, v in self.data["pitches"].items()
        ):
            raise ValueError("无效音高配置")
        collections = self.data["collections"]
        if not isinstance(collections, list) or not all(
            isinstance(c, dict)
            and isinstance(c.get("name"), str)
            and isinstance(c.get("files"), list)
            and all(isinstance(f, str) for f in c["files"])
            for c in collections
        ):
            raise ValueError("无效收藏夹配置")
        for field in ("current", "cloud_url", "update_repo"):
            if not isinstance(self.data[field], str):
                raise ValueError(f"无效配置字段: {field}")

    def save(self):
        atomic_json(self.path, self.data)

    def import_score(self, path: Path) -> Score:
        score = load_score(path)
        score.save(unique_path(self.songs_dir, score.name))
        return score

    def add_score(self, score: Score) -> Score:
        score.save(unique_path(self.songs_dir, score.name))
        return score

    def is_favorite(self, path: Path) -> bool:
        return any(str(path) in c["files"] for c in self.data["collections"])

    def toggle_favorite(self, path: Path, collection: int = 0):
        if not self.data["collections"]:
            self.data["collections"].append({"name": "我的收藏", "files": []})
        files = self.data["collections"][collection]["files"]
        value = str(path)
        files.remove(value) if value in files else files.append(value)
        self.save()

    def remove(self, path: Path):
        # Move to recoverable trash; callers never delete an imported source.
        if path.resolve().parent != self.songs_dir.resolve():
            raise ValueError("只能移除曲库目录内的文件")
        trash = self.directory / "trash"
        path.rename(unique_path(trash, path.stem, path.suffix))
        for collection in self.data["collections"]:
            collection["files"] = [f for f in collection["files"] if f != str(path)]
        self.data["queue"] = [f for f in self.data["queue"] if f != str(path)]
        self.save()


@dataclass
class LibraryResult:
    scores: list[Score]
    errors: list[tuple[Path, str]]


def scan_library(directory: Path) -> LibraryResult:
    scores, errors = [], []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() in (".json", ".txt") and path.is_file():
            try:
                scores.append(load_score(path))
            except (OSError, ValueError) as exc:
                errors.append((path, str(exc)))
    return LibraryResult(sorted(scores, key=lambda s: s.name.casefold()), errors)
