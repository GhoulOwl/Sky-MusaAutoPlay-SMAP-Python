"""Sky Studio/SMAP score format. Time is always absolute milliseconds.

Keeping milliseconds avoids changing imported music through BPM rounding.
Unknown metadata, other array entries, and per-note fields survive editing.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ScoreError(ValueError):
    pass


@dataclass
class Note:
    key: int
    time_ms: float
    duration_ms: float = 180
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if type(self.key) is not int or not 0 <= self.key < 15:
            raise ScoreError("音符键位必须在 0–14 之间")
        if not math.isfinite(self.time_ms) or not 0 <= self.time_ms <= 86_400_000:
            raise ScoreError("音符时间必须在 0–24 小时之间")
        if not math.isfinite(self.duration_ms) or not 0 < self.duration_ms <= 600_000:
            raise ScoreError("音符时长无效")


@dataclass
class Score:
    name: str = "未命名"
    bpm: float = 120
    author: str = ""
    transcriber: str = ""
    notes: list[Note] = field(default_factory=list)
    root: dict[str, Any] = field(default_factory=dict)
    tail: list[Any] = field(default_factory=list)
    path: Path | None = None

    @property
    def duration_ms(self) -> float:
        return max((n.time_ms + n.duration_ms for n in self.notes), default=0)

    @property
    def ms_per_beat(self) -> float:
        return 60_000 / self.bpm

    def groups(self) -> list[tuple[float, tuple[int, ...]]]:
        groups: dict[float, set[int]] = {}
        for note in self.notes:
            groups.setdefault(note.time_ms, set()).add(note.key)
        return [(t, tuple(sorted(keys))) for t, keys in sorted(groups.items())]

    def to_data(self) -> list[Any]:
        root = copy.deepcopy(self.root)
        root.update(
            name=self.name, author=self.author, transcribedBy=self.transcriber, bpm=self.bpm, keyCount=15
        )
        for key, val in {
            "isComposed": True,
            "isEncrypted": False,
            "bitsPerPage": 16,
            "pitchLevel": 0,
        }.items():
            root.setdefault(key, val)
        root["songNotes"] = [
            dict(
                copy.deepcopy(n.extra),
                time=round(n.time_ms, 6),
                key=f"1Key{n.key}",
                duration=round(n.duration_ms, 6),
            )
            for n in sorted(self.notes, key=lambda n: (n.time_ms, n.key))
        ]
        return [root, *copy.deepcopy(self.tail)]

    def save(self, path: Path | str | None = None):
        target = Path(path) if path else self.path
        if target is None:
            raise ScoreError("尚未指定保存位置")
        atomic_json(target, self.to_data())
        self.path = target.resolve()

    def set_bpm(self, bpm: float, scale_times: bool = False):
        if not math.isfinite(bpm) or not 1 <= bpm <= 2000:
            raise ScoreError("BPM 必须在 1–2000 之间")
        if scale_times:
            ratio = self.bpm / bpm
            for note in self.notes:
                note.time_ms *= ratio
                note.duration_ms *= ratio
        self.bpm = bpm


def atomic_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, allow_nan=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data[:4] in (b"[\x00{\x00", b'{\x00"\x00'):
        return data.decode("utf-16-le")
    if data[:4] in (b"\x00[\x00{", b'\x00{\x00"'):
        return data.decode("utf-16-be")
    return data.decode("utf-8-sig")


def parse_key(value: Any) -> int:
    if type(value) is int and 0 <= value < 15:
        return value
    if isinstance(value, str) and (m := re.fullmatch(r"(?:\d*Key)?(\d+)\s*", value)):
        key = int(m[1])
        if 0 <= key < 15:
            return key
    raise ScoreError(f"无效键位: {value!r}")


def parse_score(data: bytes | str, name: str = "未命名") -> Score:
    try:
        value = json.loads(decode_text(data) if isinstance(data, bytes) else data)
        tail = value[1:] if isinstance(value, list) else []
        root = value[0] if isinstance(value, list) and value else value
        if not isinstance(root, dict):
            raise ScoreError("曲谱根节点须为对象或包含对象的数组")
        if root.get("isEncrypted"):
            raise ScoreError("此曲谱已加密，请先使用原工具解密")
        if root.get("keyCount", 15) not in (15, "15"):
            raise ScoreError("仅支持光遇 15 键曲谱")
        raw_notes = root.get("songNotes")
        if not isinstance(raw_notes, list):
            raise ScoreError("曲谱缺少 songNotes 数组")
        bpm = float(root.get("bpm", 120))
        if not math.isfinite(bpm) or not 1 <= bpm <= 2000:
            bpm = 120
        notes = []
        for index, obj in enumerate(raw_notes):
            try:
                if not isinstance(obj, dict) or isinstance(obj.get("time"), bool):
                    raise ScoreError("无效音符对象")
                notes.append(
                    Note(
                        parse_key(obj["key"]),
                        float(obj["time"]),
                        float(obj.get("duration", 180)),
                        copy.deepcopy(obj),
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                raise ScoreError(f"第 {index + 1} 个音符错误: {exc}") from exc
        notes.sort(key=lambda n: (n.time_ms, n.key))
        return Score(
            str(root.get("name") or name),
            bpm,
            str(root.get("author") or ""),
            str(root.get("transcribedBy") or ""),
            notes,
            copy.deepcopy(root),
            tail,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, OverflowError) as exc:
        raise ScoreError(f"无法读取曲谱: {exc}") from exc


def load_score(path: Path | str) -> Score:
    path = Path(path)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ScoreError("曲谱文件超过 20 MB")
    score = parse_score(path.read_bytes(), path.stem)
    score.path = path.resolve()
    return score


def safe_name(name: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:100] or "未命名"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", text, re.I):
        text = "_" + text
    return text


def unique_path(directory: Path, name: str, suffix: str = ".json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    name = safe_name(name)
    target = directory / (name + suffix)
    index = 2
    while target.exists():
        target = directory / f"{name} ({index}){suffix}"
        index += 1
    return target
