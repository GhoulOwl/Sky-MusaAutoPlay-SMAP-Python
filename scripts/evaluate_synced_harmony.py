"""Frozen v3 versus melody-synchronized v4, with a three-key total budget."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from evaluate_balanced_harmony import HTML, TONES, assert_balance, audit, identity, link
from evaluate_muscriptor import render

from autoskyplay.audio import INSTRUMENTS, SAMPLE_RATE
from autoskyplay.score import load_score
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.events import EventResult

BEFORE = Path('artifacts/phrased-harmony-2026-10-20')
OUTPUT = Path('artifacts/synced-harmony-2026-10-20')


def timing_audit(score, beat, start=0., end=float('inf')):
    lead = [n for n in score.notes if n.extra['role'] == 'melody']
    support = [n for n in score.notes if n.extra['role'] == 'accompaniment' and start <= n.time_ms < end]
    aligned = off = independent = 0
    for n in support:
        if any(abs(m.time_ms-n.time_ms) < 1e-3 for m in lead):
            aligned += 1
        elif (any(m.time_ms <= n.time_ms < m.time_ms+m.duration_ms for m in lead) or
              any(a.time_ms+a.duration_ms <= n.time_ms < b.time_ms and
                  b.time_ms-a.time_ms-a.duration_ms <= max(700., 1.5*beat) for a, b in zip(lead, lead[1:]))):
            off += 1
        else:
            independent += 1
    return dict(aligned_support=aligned, off_melody_support=off, independent_support=independent,
                maximum_alignment_shift_ms=max((abs(n.extra.get('alignmentOffsetMs', 0)) for n in support), default=0.))


def main():
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT/'media').mkdir(exist_ok=True)
    for name, expected in json.loads((OUTPUT/'baseline-sha256.json').read_text()).items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, name
    old = json.loads((BEFORE/'report.json').read_text())
    report = dict(cases=old['cases'], windows=old['windows'], scores=[], windows_audit=[], all_instruments=[],
                  volume=.42, reverb=False, version='rich-harmony-4', baseline='rich-harmony-3',
                  interpretation='Objective timing, sample-power and envelope checks; not human listening scores.')
    for case in report['cases']:
        song, title = case['id'], case['title']
        source = Path(f'artifacts/hybrid-2026-10-14/full-{song}/events.json')
        assert hashlib.sha256(source.read_bytes()).hexdigest() == case['events_sha256']
        result = EventResult.load(source)
        beats = result.analysis.get('beats_ms', [])
        beat = float(np.median(np.diff(beats))) if len(beats) >= 4 else 60000/result.bpm
        link(BEFORE/'media'/f'{song}-original.flac', OUTPUT/'media'/f'{song}-original.flac')
        for tone in INSTRUMENTS:
            score, _ = build_score(result, title, instrument=tone, mode='rich_chords')
            reference, _ = build_score(result, title, instrument=tone, mode='melody')
            assert identity(score) == identity(reference), (song, tone, 'melody changed')
            metrics = dict(assert_balance(score), **timing_audit(score, beat))
            assert metrics['maximum_keys'] <= 3 and metrics['off_melody_support'] == 0, (song, tone, metrics)
            report['all_instruments'].append(dict(song=song, instrument=tone, **metrics))
            if tone not in TONES:
                continue
            before = load_score(BEFORE/f'{song}-{tone}-after.json')
            assert identity(before) == identity(score), (song, tone, 'baseline melody changed')
            link(BEFORE/f'{song}-{tone}-after.json', OUTPUT/f'{song}-{tone}-before.json')
            score.save(OUTPUT/f'{song}-{tone}-after.json')
            for mode, old_mode in [('before', 'after'), ('before-support', 'support'), ('melody', 'melody')]:
                link(BEFORE/'media'/f'{song}-{tone}-{old_mode}.flac', OUTPUT/'media'/f'{song}-{tone}-{mode}.flac')
            samples = render(score.notes, result.duration_ms/1000+3, tone)
            peak, clipped = float(np.max(np.abs(samples))), int(np.sum(np.abs(samples) >= 1))
            assert clipped == 0, (song, tone, peak)
            sf.write(OUTPUT/'media'/f'{song}-{tone}-after.flac', samples, SAMPLE_RATE, subtype='PCM_16')
            del samples
            support = render([n for n in score.notes if n.extra['role'] == 'accompaniment'],
                             result.duration_ms/1000+3, tone)
            sf.write(OUTPUT/'media'/f'{song}-{tone}-support.flac', support, SAMPLE_RATE, subtype='PCM_16')
            lead_audio, sr = sf.read(OUTPUT/'media'/f'{song}-{tone}-melody.flac', dtype='float32', always_2d=True)
            old_support, old_sr = sf.read(OUTPUT/'media'/f'{song}-{tone}-before-support.flac', dtype='float32', always_2d=True)
            assert sr == old_sr == SAMPLE_RATE
            for mode, current, accompaniment in [('before', before, old_support), ('after', score, support)]:
                row = dict(song=song, instrument=tone, mode=mode, melody_unchanged=True,
                           **audit(current), **timing_audit(current, beat))
                if mode == 'after':
                    row.update(peak=peak, clipped_samples=clipped)
                report['scores'].append(row)
                for w in (w for w in report['windows'] if w['song'] == song):
                    a, b = round(w['start']*SAMPLE_RATE), round(w['end']*SAMPLE_RATE)
                    power, lead_power = float(np.mean(accompaniment[a:b]**2)), float(np.mean(lead_audio[a:b]**2))
                    row = dict(window=w['id'], instrument=tone, mode=mode,
                        **audit(current, w['start']*1000, w['end']*1000),
                        **timing_audit(current, beat, w['start']*1000, w['end']*1000),
                        support_to_lead_db=round(10*np.log10(max(power, 1e-12)/max(lead_power, 1e-12)), 2))
                    if mode == 'after':
                        assert row['accompaniment'] > 0, (song, tone, w['id'], 'empty support')
                        assert row['support_to_lead_db'] <= -2., (song, tone, w['id'], row)
                    report['windows_audit'].append(row)
            print(song, tone, json.dumps(metrics), 'peak', round(peak, 3), flush=True)
            del support, lead_audio, old_support
        print(song, '37 instruments passed', flush=True)
    (OUTPUT/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    write_page(report)


def write_page(report):
    html = HTML.replace('伴奏平衡 · 前后对照', '同步和弦 · 前后对照')
    html = html.replace('主旋律＋和弦 · 伴奏平衡对照', '主旋律＋和弦 · 同步起音与三键限制')
    html = html.replace('对比上一次密集版与本次平衡版。主旋律保持；伴奏控制音区、起音和强音尾叠响。',
        '对比上一版乐句编配与本次同步版。有旋律时伴奏对齐旋律起音，总共最多三键；独立器乐段保留原节奏。')
    html = html.replace('修改前 · 密集版', '修改前 · 乐句版').replace('修改后 · 平衡版', '修改后 · 同步版')
    html = html.replace("?'平衡版':'密集版'", "?'同步版':'前版乐句'")
    html = html.replace('<option value="support">', '<option value="before-support">修改前 · 伴奏单听</option><option value="support">')
    html = html.replace('<th>旋律期间最多强伴奏音尾</th>', '<th>最多同时按键</th><th>旋律段错开起音</th><th>同步伴奏音</th>')
    html = html.replace("x.maximum_strong_support_with_lead+'</td></tr>'",
        "x.maximum_keys+'</td><td>'+x.off_melody_support+'</td><td>'+x.aligned_support+'</td></tr>'")
    html = html.replace('平衡版伴奏强音尾期间至少低于旋律 3 个半音。',
        '同步版伴奏强音尾期间至少低于旋律 3 个半音；所有伴奏与旋律合计最多三键。')
    html = html.replace('强音尾并发数依据采样包络估计。',
        '错开起音统计覆盖旋律持音与短换气；长期没有旋律的独立器乐段单独保留原节奏。强音尾依据采样包络估计。')
    (OUTPUT/'index.html').write_text(html.replace('REPORT_DATA', json.dumps(report, ensure_ascii=False).replace('</', '<\\/')))


if __name__ == '__main__':
    main()
