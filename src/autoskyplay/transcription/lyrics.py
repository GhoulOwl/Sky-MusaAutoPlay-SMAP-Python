"""Parse NetEase lyric formats and locate lines on the original song timeline."""
from __future__ import annotations

import bisect
import re

_LINE = re.compile(r'^\[(\d+),(\d+)\](.*)$')
_WORD = re.compile(r'\((\d+),(\d+),\d+\)(.*?)(?=\(\d+,\d+,\d+\)|$)')
_STAMP = re.compile(r'\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]')
_OFFSET = re.compile(r'^\[offset:([+-]?\d+)\]$', re.I | re.M)


def parse_yrc(raw):
    lines = []
    for row in raw.splitlines():
        match = _LINE.match(row.strip())
        if not match:
            continue
        start, duration = int(match[1]), int(match[2])
        words = [{'start': int(w[1]), 'duration': int(w[2]), 'text': w[3]}
                 for w in _WORD.finditer(match[3]) if w[3]]
        text = ''.join(word['text'] for word in words).strip()
        if text and duration > 0:
            lines.append(dict(start=start, duration=duration, text=text, words=words))
    return sorted(lines, key=lambda x: x['start'])


def parse_lrc(raw):
    offset_match = _OFFSET.search(raw)
    offset = int(offset_match[1]) if offset_match else 0
    lines = []
    for row in raw.splitlines():
        stamps = list(_STAMP.finditer(row))
        if not stamps:
            continue
        text = _STAMP.sub('', row).strip()
        if not text:
            continue
        for stamp in stamps:
            fraction = (stamp[3] or '').ljust(3, '0')
            start = max(0, (int(stamp[1]) * 60 + int(stamp[2])) * 1000 + int(fraction or 0) + offset)
            lines.append(dict(start=start, duration=0, text=text, words=[]))
    return sorted(lines, key=lambda x: x['start'])


def lyric_line(lyrics, position_ms):
    lines = lyrics.get('lines', []) if isinstance(lyrics, dict) else []
    index = bisect.bisect_right([line['start'] for line in lines], position_ms) - 1
    return lines[index] if index >= 0 else None


def valid_lyrics(value, song_id):
    """Reject malformed lyric metadata in imported scores before displaying it."""
    if not isinstance(value, dict) or value.get('song_id') != song_id:
        return False
    if value.get('format') not in ('yrc', 'lrc', 'none') or not isinstance(value.get('lines'), list):
        return False
    previous = -1
    for line in value['lines']:
        if not isinstance(line, dict):
            return False
        start = line.get('start')
        if type(start) is not int or start < previous or not isinstance(line.get('text'), str):
            return False
        previous = start
        words = line.get('words', [])
        if not isinstance(words, list):
            return False
        if any(not isinstance(word, dict) or type(word.get('start')) is not int
               or not isinstance(word.get('text'), str) for word in words):
            return False
    return True
