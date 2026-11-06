"""Rebuild the six local MOSS direct/optimized listening comparisons."""
from __future__ import annotations

import argparse
import html
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import soundfile as sf

from autoskyplay.audio import SAMPLE_RATE
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.audition import render_window
from autoskyplay.transcription.events import EventResult
from autoskyplay.transcription.moss import parse_lines
from autoskyplay.transcription.rich_harmony import strong_tails


def statistics(score):
    lead = [n for n in score.notes if n.extra.get('role') == 'melody']
    support = [n for n in score.notes if n.extra.get('source') == 'moss']
    tails = strong_tails('Piano')
    overlaps = [n for n in support if any(
        n.key == m.key and n.time_ms < m.time_ms+m.duration_ms
        and m.time_ms < n.time_ms+n.duration_ms for m in lead)]
    strong = [n for n in support if any(
        n.key == m.key and n.time_ms < m.time_ms+max(m.duration_ms, tails[n.key])
        and m.time_ms < n.time_ms+max(n.duration_ms, tails[n.key]) for m in lead)]
    starts = sorted({n.time_ms for n in score.notes})
    return dict(accompaniment=len(support), approximated=sum(bool(n.extra.get('approximated')) for n in support),
                lead_key_overlaps=len(overlaps), strong_tail_overlaps=len(strong),
                doubled=sum(bool(n.extra.get('mossDoubled')) for n in support),
                maximum_keys=max((len({n.key for n in score.notes if n.time_ms <= t < n.time_ms+n.duration_ms})
                                  for t in starts), default=0),
                total_support_ms=round(sum(n.duration_ms for n in support)))


def lead_identity(score):
    return [(n.key, n.time_ms, n.duration_ms, n.extra.get('sourcePitch')) for n in score.notes
            if n.extra.get('role') == 'melody']


def listening_page(rows, output):
    cards = []
    for row in rows:
        song, offset = row['song'], row['offset']
        direct, optimized = row['direct'], row['optimized']
        source = f'{song}-24s.wav' if offset == 0 else f'{song}-60s.wav'
        players = []
        for title, filename in (('原曲', source), ('MOSS 直接版', f'{song}-{offset}-moss-direct.wav'),
                                ('MOSS 优化版', f'{song}-{offset}-moss-optimized.wav')):
            if (output / filename).is_file():
                players.append(f'<label>{title}<audio controls preload="none" src="{html.escape(filename)}"></audio></label>')
        cards.append(f'''<section><h2>{html.escape(song)} · {offset}–{offset+24} 秒</h2>
<p>伴奏音符 {direct['accompaniment']} → {optimized['accompaniment']} · 同键重叠 {direct['lead_key_overlaps']} → {optimized['lead_key_overlaps']} · 半音近似 {direct['approximated']} → {optimized['approximated']}</p>
<div class="players">{''.join(players)}</div></section>''')
    page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>MOSS 和弦对照试听</title>
<style>body{font:16px/1.6 system-ui,sans-serif;background:#f5f3ef;color:#242629;margin:0}
main{max-width:1050px;margin:auto;padding:32px 20px 60px}h1{margin:0 0 8px;font-size:30px}h2{font-size:20px;margin:0}
section{background:white;border:1px solid #dedbd4;border-radius:14px;padding:20px;margin:18px 0}
section p{color:#565b60}.players{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:15px}
label{display:grid;gap:8px;font-weight:600}audio{width:100%}.note{color:#565b60}</style></head><body><main>
<h1>MOSS 和弦对照试听</h1><p class="note">同一段歌曲的直接版与优化版使用相同钢琴音色和混音音量。原曲音量未匹配；统计是音符事件，不代表听感或准确率。</p>
''' + '\n'.join(cards) + '</main></body></html>'
    (output / 'optimized-listening.html').write_text(page, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/moss-experiment'))
    parser.add_argument('--events-root', type=Path, default=Path('artifacts/muscriptor-2026-10-14'))
    args = parser.parse_args()
    out = args.output
    rows = []
    for song in ('love', 'island', 'zugou'):
        for offset in (0, 60):
            full = EventResult.load(args.events_root / f'small-{song}' / 'events.json')
            left, right = offset*1000, (offset+24)*1000
            notes = [replace(n, start_ms=max(n.start_ms, left)-left,
                             duration_ms=min(n.start_ms+n.duration_ms, right)-max(n.start_ms, left))
                     for n in full.raw_notes if n.start_ms < right and n.start_ms+n.duration_ms > left]
            analysis = dict(beats_ms=[b-left for b in full.analysis['beats_ms'] if left <= b <= right],
                            beat_confidence=full.analysis['beat_confidence'],
                            energy_db=full.analysis['energy_db'][offset*10:(offset+24)*10])
            result = EventResult(notes, 24000, full.bpm, dict(full.metadata), analysis)
            stem = f'{song}-diagnostic' if offset == 0 else f'{song}-60-diagnostic'
            result.analysis['moss_music'] = parse_lines((out/f'{stem}.prose.txt').read_text('utf-8'),
                                                        (out/f'{stem}.mm3.txt').read_text('utf-8'), 24000)
            scores = {}
            for name, mode in (('direct', 'moss_direct'), ('optimized', 'moss_optimized')):
                score, _ = build_score(result, name, mode=mode, instrument='Piano', shift=0, rhythm='original')
                scores[name] = score
                audio = render_window(score, 0, 24000)
                sf.write(out/f'{song}-{offset}-moss-{name}.wav', audio, SAMPLE_RATE)
                if name == 'optimized':
                    peak = float(np.max(np.abs(audio)))
                    clipped = int((np.abs(audio) >= 1).sum())
            row = dict(song=song, offset=offset, direct=statistics(scores['direct']),
                       optimized=statistics(scores['optimized']),
                       melody_unchanged=lead_identity(scores['direct']) == lead_identity(scores['optimized']),
                       metadata=scores['optimized'].root['transcription']['moss_optimized'],
                       peak=peak, clipped_samples=clipped)
            rows.append(row)
            print(json.dumps({k: v for k, v in row.items() if k != 'metadata'}, ensure_ascii=False), flush=True)
    (out/'optimized-comparison.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    listening_page(rows, out)


if __name__ == '__main__':
    main()
