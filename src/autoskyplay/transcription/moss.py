"""Experimental MOSS-Music analysis; all timestamps are relative to the selected audio."""
from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

import imageio_ffmpeg

CHORD_PROMPT = '''Listen carefully to the music. Identify the chord progression and the time at which each chord starts, using timestamps from the beginning of this recording. Write one line per chord in the form "00:00 C major". Only include chords you hear; do not invent any. The recording ends at {duration:.2f} seconds.'''
SECTION_PROMPT = '''Listen to the music and identify the structure of this recording. For each musical section give its start and end time and its name (intro, verse, chorus, bridge, instrumental, or outro). The recording lasts {duration:.2f} seconds. Write one line per section as "00:00 - 00:10 intro". Only report sections you can hear.'''
PITCHES = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
LABELS = {'intro': '前奏', 'verse': '主歌', 'chorus': '副歌', 'bridge': '桥段',
          'instrumental': '器乐段', 'outro': '尾奏'}
TIME = r'(?:\d+:)?\d+(?:\.\d+)?'
CHORD_INTERVALS = {
    '': (0, 4, 7), 'maj': (0, 4, 7), 'major': (0, 4, 7),
    'm': (0, 3, 7), 'min': (0, 3, 7), 'minor': (0, 3, 7),
    '7': (0, 4, 7, 10), 'maj7': (0, 4, 7, 11), 'm7': (0, 3, 7, 10),
    'min7': (0, 3, 7, 10), 'm7b5': (0, 3, 6, 10),
    'dim': (0, 3, 6), 'dim7': (0, 3, 6, 9), 'aug': (0, 4, 8),
    'sus2': (0, 2, 7), 'sus4': (0, 5, 7), 'sus': (0, 5, 7),
    '6': (0, 4, 7, 9), 'm6': (0, 3, 7, 9), 'add9': (0, 4, 7, 14),
    'maj9': (0, 4, 7, 11, 14), 'm9': (0, 3, 7, 10, 14), '9': (0, 4, 7, 10, 14),
    '11': (0, 4, 7, 10, 14, 17), '13': (0, 4, 7, 10, 14, 21),
    '7b9': (0, 4, 7, 10, 13), '7#9': (0, 4, 7, 10, 15), '7b5': (0, 4, 6, 10),
}


def parse_chord_symbol(symbol):
    """Interpret known chord symbols without silently simplifying unknown ones."""
    match = re.fullmatch(r'([A-G])([#b]?)([^/]*)(?:/([A-G])([#b]?))?', symbol.strip(), re.I)
    if not match:
        return None
    root = (PITCHES[match[1].upper()] + {'': 0, '#': 1, 'b': -1}[match[2].lower()]) % 12
    suffix = match[3].strip()
    quality = {'M': 'maj', 'M7': 'maj7'}.get(suffix, suffix.lower())
    intervals = CHORD_INTERVALS.get(quality)
    bass = ((PITCHES[match[4].upper()] + {'': 0, '#': 1, 'b': -1}[match[5].lower()]) % 12
            if match[4] else None)
    return dict(root=root, intervals=list(intervals) if intervals is not None else None, bass=bass)


def seconds(text):
    parts = [float(x) for x in text.split(':')]
    if len(parts) == 2 and parts[1] >= 60:
        raise ValueError('MOSS 时间格式无效')
    return parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0]


def parse_lines(chord_text, section_text, duration_ms):
    """Bound uncertain free-text timestamps; never turn extensions into triads."""
    if len(chord_text) + len(section_text) > 100_000:
        raise ValueError('MOSS 输出过长')
    rows = []
    for line in chord_text.splitlines():
        match = re.fullmatch(rf'\s*\[?({TIME})\s*(?:s|sec)?\]?\s*[:\-]?\s*([A-G][#b]?(?:\s*(?:major|minor)|[A-Za-z0-9+/#-]*)?)\s*', line, re.I)
        if match:
            start = seconds(match[1]) * 1000
            name = match[2].strip()
            rows.append((start, name))
        else:
            silence = re.fullmatch(rf'\s*\[?({TIME})\s*(?:s|sec)?\]?\s*[:\-]?\s*N\.?C\.?\s*', line, re.I)
            if silence:
                rows.append((seconds(silence[1]) * 1000, 'N.C.'))
    rows.sort()
    if any(a[0] == b[0] for a, b in zip(rows, rows[1:])):
        raise ValueError('MOSS 和弦时间重复')
    chords, raw_chords = [], []
    for i, (start, name) in enumerate(rows):
        if not 0 <= start < duration_ms:
            continue
        parsed = parse_chord_symbol(name)
        end = min(duration_ms, rows[i+1][0] if i+1 < len(rows) else duration_ms)
        if parsed and end > start:
            raw_chords.append(dict(start_ms=start, end_ms=end, symbol=name, **parsed,
                                   inferred_end=i+1 == len(rows) or rows[i+1][0] >= duration_ms))
        match = re.fullmatch(r'([A-G])([#b]?)(m|min|maj|\s+major|\s+minor)?', name, re.I)
        if not match:
            continue
        end = min(duration_ms, rows[i+1][0] if i+1 < len(rows) else start + 4000)
        if end <= start:
            continue
        root = (PITCHES[match[1].upper()] + {'': 0, '#': 1, 'b': -1}[match[2].lower()]) % 12
        quality = 'minor' if (match[3] or '').strip().lower() in ('m', 'min', 'minor') else 'major'
        chords.append(dict(start_ms=start, end_ms=end, root=root, quality=quality))
    sections = []
    for line in section_text.splitlines():
        match = re.fullmatch(rf'\s*\[?({TIME})\s*(?:s|sec)?\]?\s*[-–—]\s*\[?({TIME})\s*(?:s|sec)?\]?\s*[:\-]?\s*(intro|verse|chorus|bridge|instrumental|outro)\s*', line, re.I)
        if match:
            start, end = seconds(match[1]) * 1000, min(duration_ms, seconds(match[2]) * 1000)
            if 0 <= start < end:
                sections.append(dict(start_ms=start, end_ms=end, label=match[3].lower()))
    sections.sort(key=lambda x: x['start_ms'])
    result = dict(chords=chords, sections=sections, raw_chords=raw_chords)
    if not raw_chords and not sections:
        raise ValueError('MOSS 输出中没有可用的和弦或段落，请重试；原草稿未改变')
    validate_analysis(result, duration_ms)
    return result


def validate_analysis(data, duration_ms):
    if not isinstance(data, dict) or not {'chords', 'sections'} <= set(data) or set(data) - {'chords', 'sections', 'raw_chords'}:
        raise ValueError('MOSS 分析格式无效')
    for field in ('chords', 'sections', 'raw_chords'):
        items = data.get(field, [])
        if not isinstance(items, list) or len(items) > 1000:
            raise ValueError('MOSS 分析格式无效')
        previous = 0
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('MOSS 分析格式无效')
            a, b = item.get('start_ms'), item.get('end_ms')
            if (type(a) not in (float, int) or type(b) not in (float, int)
                    or not math.isfinite(a) or not math.isfinite(b) or a < previous - 1
                    or a < 0 or b <= a or b > duration_ms):
                raise ValueError('MOSS 分析时间无效')
            if field == 'chords':
                if type(item.get('root')) is not int or item['root'] not in range(12) or item.get('quality') not in ('major', 'minor'):
                    raise ValueError('MOSS 和弦格式无效')
            elif field == 'raw_chords':
                symbol = item.get('symbol')
                parsed = parse_chord_symbol(symbol) if isinstance(symbol, str) and len(symbol) <= 80 else None
                if (not parsed or any(item.get(k) != v for k, v in parsed.items())
                        or type(item.get('inferred_end')) is not bool):
                    raise ValueError('MOSS 原始和弦格式无效')
            elif item.get('label') not in LABELS:
                raise ValueError('MOSS 段落格式无效')
            previous = b



class MossTask:
    def __init__(self, exe, models, audio, start, duration, done):
        self.exe, self.models, self.audio = map(Path, (exe, models, audio))
        self.start, self.duration, self.done = start, duration, done
        self.cancelled = threading.Event()
        self.process = None
        self.process_lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def cancel(self):
        self.cancelled.set()
        with self.process_lock:
            if self.process and self.process.poll() is None:
                try:
                    self.process.kill()
                except (ProcessLookupError, PermissionError):
                    pass  # It may have exited between poll() and kill().

    def run_process(self, args):
        with tempfile.TemporaryFile() as log:
            with self.process_lock:
                if self.cancelled.is_set():
                    raise ValueError('MOSS 分析已取消')
                self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=log,
                                                start_new_session=os.name != 'nt',
                                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                process = self.process
            code = process.wait()
            with self.process_lock:
                self.process = None
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 2000))
            details = log.read().decode('utf-8', errors='replace').strip()
        if self.cancelled.is_set():
            raise ValueError('MOSS 分析已取消')
        if code:
            raise ValueError(f'MOSS 运行失败（退出码 {code}）：{details or "请检查运行组件和模型"}')

    def run(self):
        try:
            lm = self.models / 'moss-lm-q4_k_m.gguf'
            if not all(p.is_file() for p in (self.exe, lm, self.models / 'moss-aud-f16.gguf', self.audio)):
                raise ValueError('缺少 ace-caption、Q4 权重、音频编码器或源音频')
            with tempfile.TemporaryDirectory(prefix='autosky-moss-') as tmp:
                tmp = Path(tmp)
                clip, output = tmp / 'clip.wav', tmp / 'output.txt'
                chord_prompt, section_prompt = tmp / 'chords.txt', tmp / 'sections.txt'
                chord_prompt.write_text(CHORD_PROMPT.format(duration=self.duration), encoding='utf-8')
                section_prompt.write_text(SECTION_PROMPT.format(duration=self.duration), encoding='utf-8')
                self.run_process([imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-y', '-ss', str(self.start),
                                  '-i', str(self.audio), '-t', str(self.duration), '-ac', '1', '-ar', '16000', str(clip)])
                with wave.open(str(clip), 'rb') as audio:
                    if abs(audio.getnframes() / audio.getframerate() - self.duration) > .1:
                        raise ValueError('源音频选段不完整，无法执行 MOSS 分析')
                self.run_process([str(self.exe), '--family', 'moss', '--models', str(self.models), '--lm', str(lm),
                                  '--src-audio', str(clip), '--mode', 'prose,mm3',
                                  '--prompt-file', 'prose=' + str(chord_prompt),
                                  '--prompt-file', 'mm3=' + str(section_prompt), '-o', str(output),
                                  '--max-seconds', str(self.duration), '--max-tokens', '2048', '--temperature', '0'])
                result = parse_lines(output.with_name('output.prose.txt').read_text(encoding='utf-8'),
                                     output.with_name('output.mm3.txt').read_text(encoding='utf-8'),
                                     self.duration * 1000)
                if self.cancelled.is_set():
                    raise ValueError('MOSS 分析已取消')
            self.done(result, '')
        except (OSError, ValueError, wave.Error) as exc:
            self.done(None, str(exc))
