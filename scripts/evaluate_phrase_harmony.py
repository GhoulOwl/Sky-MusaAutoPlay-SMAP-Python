"""Frozen v2 versus phrase-aware v3, using the same three recordings/events."""
from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from pathlib import Path

import numpy as np
import soundfile as sf
from evaluate_balanced_harmony import HTML, TONES, assert_balance, audit, identity, link
from evaluate_muscriptor import render

from autoskyplay.audio import INSTRUMENTS, SAMPLE_RATE
from autoskyplay.midi import SKY_MIDI
from autoskyplay.score import load_score
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.events import EventResult
from autoskyplay.transcription.harmonic_structure import pulse_from_evidence
from autoskyplay.transcription.rich_harmony import evidence_notes

BEFORE = Path('artifacts/balanced-harmony-2026-10-20')
OUTPUT = Path('artifacts/phrased-harmony-2026-10-20')


def structure(score, pulse, boundaries):
    previous, motions, accents = {}, [], dict(strong=0, pulse=0, offbeat=0, source=0)
    groups = []
    for note in score.notes:
        if note.extra['role'] != 'accompaniment':
            continue
        if not groups or note.time_ms-groups[-1][0].time_ms > 40:
            groups.append([])
        groups[-1].append(note)
        accent, beat, _strength = pulse.at(note.time_ms)
        accents[accent] += 1
        voice = note.extra.get('voice', 'bass')
        phrase = bisect_right(boundaries, note.time_ms)-1
        pitch = SKY_MIDI[note.key]
        old = previous.get(voice)
        if old and old[2] == phrase and note.time_ms-old[0] <= max(1800., 3*beat):
            motions.append(abs(pitch-old[1]))
        previous[voice] = note.time_ms, pitch, phrase
    # An independent audible-contour proxy avoids crediting smoother motion
    # merely because the new arranger assigned different bass/inner labels.
    contour, old = [], None
    for group in groups:
        time = group[0].time_ms
        pitch = min(SKY_MIDI[n.key] for n in group)
        phrase = bisect_right(boundaries, time)-1
        if old and old[2] == phrase and time-old[0] < 1800:
            contour.append(abs(pitch-old[1]))
        old = time, pitch, phrase
    return dict(accent_counts=accents, voice_connections=len(motions),
                mean_motion_semitones=round(float(np.mean(motions)), 3) if motions else 0,
                large_leaps=sum(m > 7 for m in motions),
                contour_connections=len(contour), contour_large_leaps=sum(m > 7 for m in contour),
                contour_mean_semitones=round(float(np.mean(contour)), 3) if contour else 0)


def main():
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT/'media').mkdir(exist_ok=True)
    baseline_hashes = json.loads((OUTPUT/'baseline-sha256.json').read_text())
    for name, expected in baseline_hashes.items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == expected, name
    old_report = json.loads((BEFORE/'report.json').read_text())
    report = dict(cases=old_report['cases'], windows=old_report['windows'], scores=[], windows_audit=[],
                  all_instruments=[], volume=.42, reverb=False, version='rich-harmony-3',
                  baseline='rich-harmony-2', interpretation='Measured power and envelope estimates, not human listening scores.')
    for case in report['cases']:
        song, title = case['id'], case['title']
        source = Path(f'artifacts/hybrid-2026-10-14/full-{song}/events.json')
        assert hashlib.sha256(source.read_bytes()).hexdigest() == case['events_sha256']
        result = EventResult.load(source)
        pulse = pulse_from_evidence(result.analysis, evidence_notes(result, 80), 60000/result.bpm)
        link(BEFORE/'media'/f'{song}-original.flac', OUTPUT/'media'/f'{song}-original.flac')
        for tone in INSTRUMENTS:
            score, _ = build_score(result, title, instrument=tone, mode='rich_chords')
            reference, _ = build_score(result, title, instrument=tone, mode='melody')
            assert identity(score) == identity(reference), (song, tone, 'melody changed')
            metrics = assert_balance(score)
            report['all_instruments'].append(dict(song=song, instrument=tone, **metrics))
            if tone not in TONES:
                continue
            before = load_score(BEFORE/f'{song}-{tone}-after.json')
            assert identity(before) == identity(score), (song, tone, 'baseline melody changed')
            link(BEFORE/f'{song}-{tone}-after.json', OUTPUT/f'{song}-{tone}-before.json')
            score.save(OUTPUT/f'{song}-{tone}-after.json')
            for mode, source_mode in [('before', 'after'), ('before-support', 'support'), ('melody', 'melody')]:
                link(BEFORE/'media'/f'{song}-{tone}-{source_mode}.flac',
                     OUTPUT/'media'/f'{song}-{tone}-{mode}.flac')
            samples = render(score.notes, result.duration_ms/1000+3, tone)
            peak = float(np.max(np.abs(samples)))
            clipped = int(np.sum(np.abs(samples) >= 1))
            assert clipped == 0, (song, tone, peak)
            sf.write(OUTPUT/'media'/f'{song}-{tone}-after.flac', samples, SAMPLE_RATE, subtype='PCM_16')
            del samples
            support = render([n for n in score.notes if n.extra['role'] == 'accompaniment'],
                             result.duration_ms/1000+3, tone)
            sf.write(OUTPUT/'media'/f'{song}-{tone}-support.flac', support, SAMPLE_RATE, subtype='PCM_16')
            lead_audio, sr = sf.read(OUTPUT/'media'/f'{song}-{tone}-melody.flac', dtype='float32', always_2d=True)
            old_support, old_sr = sf.read(OUTPUT/'media'/f'{song}-{tone}-before-support.flac',
                                          dtype='float32', always_2d=True)
            assert sr == old_sr == SAMPLE_RATE
            for mode, current, accompaniment in [('before', before, old_support), ('after', score, support)]:
                row = dict(song=song, instrument=tone, mode=mode, melody_unchanged=True, **audit(current),
                           **structure(current, pulse, score.root['transcription']['harmony_phrases']))
                if mode == 'after':
                    row.update(peak=peak, clipped_samples=clipped,
                               rhythm=score.root['transcription']['harmony_rhythm'],
                               continuity_windows=sum(c.get('continuity_resolved', False)
                                                      for c in score.root['transcription']['chords']))
                report['scores'].append(row)
                for window in (w for w in report['windows'] if w['song'] == song):
                    a, b = round(window['start']*SAMPLE_RATE), round(window['end']*SAMPLE_RATE)
                    power = float(np.mean(accompaniment[a:b]**2))
                    lead_power = float(np.mean(lead_audio[a:b]**2))
                    row = dict(window=window['id'], instrument=tone, mode=mode,
                               melody=sum(window['start']*1000 <= n.time_ms < window['end']*1000
                                          for n in current.notes if n.extra['role'] == 'melody'),
                               **audit(current, window['start']*1000, window['end']*1000),
                               support_to_lead_db=round(10*np.log10(max(power, 1e-12)/max(lead_power, 1e-12)), 2))
                    if mode == 'after':
                        assert row['accompaniment'] > 0, (song, tone, window['id'], 'empty support')
                        assert row['support_to_lead_db'] <= -2., (song, tone, window['id'], row)
                    report['windows_audit'].append(row)
            print(song, tone, json.dumps(metrics), 'peak', round(peak, 3), flush=True)
            del support, lead_audio, old_support
        print(song, '37 instruments passed', flush=True)
    (OUTPUT/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    write_page(report)


def write_page(report):
    html = HTML.replace('伴奏平衡 · 前后对照', '乐句编配 · 前后对照')
    html = html.replace('主旋律＋和弦 · 伴奏平衡对照', '主旋律＋和弦 · 乐句编配对照')
    html = html.replace('对比上一次密集版与本次平衡版。主旋律保持；伴奏控制音区、起音和强音尾叠响。',
                        '对比上一版平衡编配与本次乐句编配。主旋律保持；依据可信重音、和弦连续性与声部连接组织伴奏。')
    html = html.replace('修改前 · 密集版', '修改前 · 平衡版').replace('修改后 · 平衡版', '修改后 · 乐句版')
    html = html.replace("?'平衡版':'密集版'", "?'乐句版':'前版平衡'")
    html = html.replace('<option value="support">', '<option value="before-support">修改前 · 伴奏单听</option><option value="support">')
    html = html.replace('强音尾并发数依据采样包络估计。', '重音来自原曲音符起音证据，不等于识别了拍号或小节第一拍。强音尾并发数依据采样包络估计。')
    html = html.replace('<th>旋律期间最多强伴奏音尾</th>',
                        '<th>旋律期间最多强伴奏音尾</th><th>伴奏轮廓大跳</th>')
    html = html.replace("x.maximum_strong_support_with_lead+'</td></tr>'",
                        "x.maximum_strong_support_with_lead+'</td><td>'+x.contour_large_leaps+'</td></tr>'")
    html = html.replace('统计用于验证音区与叠响约束，听感请结合原曲和分声部对照。',
                        '伴奏轮廓大跳按同一乐句内相邻起音组的最低音统计，大于 7 个半音记一次，不依赖声部标签。统计不是人工听感评分，请结合原曲和分声部对照。')
    html = html.replace('REPORT_DATA', json.dumps(report, ensure_ascii=False).replace('</', '<\\/'))
    (OUTPUT/'index.html').write_text(html)


if __name__ == '__main__':
    main()
