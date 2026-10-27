"""Compare the saved rich-harmony-1 scores with melody-first rich-harmony-2."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import soundfile as sf
from evaluate_muscriptor import render

from autoskyplay.audio import INSTRUMENTS, SAMPLE_RATE
from autoskyplay.midi import SKY_MIDI
from autoskyplay.score import load_score
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.events import EventResult
from autoskyplay.transcription.rich_harmony import sounding_end

BEFORE = Path('artifacts/rich-harmony-2026-10-20')
OUTPUT = Path('artifacts/balanced-harmony-2026-10-20')
TONES = ('Piano', 'Guitar', 'Violin', 'Flute')


def identity(score):
    return [(n.key, round(n.time_ms, 6), round(n.duration_ms, 6), n.extra['sourcePitch'])
            for n in score.notes if n.extra['role'] == 'melody']


def audit(score, start=0., end=float('inf')):
    lead = [n for n in score.notes if n.extra['role'] == 'melody']
    support = [n for n in score.notes if n.extra['role'] == 'accompaniment']
    selected = [n for n in support if start <= n.time_ms < end]
    crossing = close = 0
    for n in selected:
        active = [m for m in lead if min(sounding_end(n), m.time_ms+m.duration_ms, end) -
                  max(n.time_ms, m.time_ms, start) > 1e-3]
        crossing += any(SKY_MIDI[n.key] >= SKY_MIDI[m.key] for m in active)
        close += any(SKY_MIDI[m.key]-SKY_MIDI[n.key] < 3 for m in active)
    points = sorted({n.time_ms for n in score.notes if start <= n.time_ms < end})
    maximum = max_support = max_support_with_lead = duplicate = 0
    for t in points:
        held = [n for n in score.notes if n.time_ms <= t+1e-3 < n.time_ms+n.duration_ms-1e-3]
        audible = [n for n in support if n.time_ms <= t+1e-3 < sounding_end(n)-1e-3]
        melody = [n for n in lead if n.time_ms <= t+1e-3 < n.time_ms+n.duration_ms-1e-3]
        maximum = max(maximum, len(held))
        max_support = max(max_support, len(audible))
        if melody:
            max_support_with_lead = max(max_support_with_lead, len(audible))
        duplicate += len(held)-len({n.key for n in held})
    # Grouped attacks distinguish a chord from several new rhythmic accents.
    attacks, last = 0, -1e9
    for n in selected:
        if n.time_ms-last > 40:
            attacks += 1
            last = n.time_ms
    return dict(accompaniment=len(selected), generated=sum(n.extra.get('generated', False) for n in selected),
                above_or_equal_lead=crossing, insufficient_lead_gap=close, attacks=attacks,
                maximum_keys=maximum, maximum_strong_support=max_support,
                maximum_strong_support_with_lead=max_support_with_lead, same_key_conflicts=duplicate)


def assert_balance(score):
    metrics = audit(score)
    assert metrics['above_or_equal_lead'] == metrics['insufficient_lead_gap'] == 0, metrics
    assert metrics['maximum_keys'] <= 4 and metrics['same_key_conflicts'] == 0, metrics
    assert metrics['maximum_strong_support_with_lead'] <= 2, metrics
    assert metrics['maximum_strong_support'] <= 3, metrics
    return metrics


def link(source, target):
    if not target.exists():
        os.link(source, target)


def main():
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT/'media').mkdir(exist_ok=True)
    old_report = json.loads((BEFORE/'report.json').read_text())
    report = dict(cases=old_report['cases'], windows=old_report['windows'], scores=[], windows_audit=[],
                  all_instruments=[], volume=.42, reverb=False, version='rich-harmony-2',
                  interpretation='Strong-tail counts are envelope estimates, not perceived loudness.')
    for case in report['cases']:
        song, title = case['id'], case['title']
        source = Path(f'artifacts/hybrid-2026-10-14/full-{song}/events.json')
        assert hashlib.sha256(source.read_bytes()).hexdigest() == case['events_sha256']
        result = EventResult.load(source)
        link(BEFORE/'media'/f'{song}-original.flac', OUTPUT/'media'/f'{song}-original.flac')
        for tone in INSTRUMENTS:
            score, _ = build_score(result, title, instrument=tone, mode='rich_chords')
            reference, _ = build_score(result, title, instrument=tone, mode='melody')
            assert identity(score) == identity(reference), (song, tone, 'melody changed')
            metrics = assert_balance(score)
            report['all_instruments'].append(dict(song=song, instrument=tone, **metrics))
            if tone not in TONES:
                continue
            before = load_score(BEFORE/f'{song}-{tone}-rich_chords.json')
            assert identity(before) == identity(score), (song, tone, 'baseline melody changed')
            link(BEFORE/f'{song}-{tone}-rich_chords.json', OUTPUT/f'{song}-{tone}-before.json')
            score.save(OUTPUT/f'{song}-{tone}-after.json')
            for mode in ('before', 'melody'):
                old_mode = 'rich_chords' if mode == 'before' else mode
                link(BEFORE/'media'/f'{song}-{tone}-{old_mode}.flac', OUTPUT/'media'/f'{song}-{tone}-{mode}.flac')
            samples = render(score.notes, result.duration_ms/1000+3, tone)
            peak = float(np.max(np.abs(samples)))
            clipped = int(np.sum(np.abs(samples) >= 1))
            assert clipped == 0
            sf.write(OUTPUT/'media'/f'{song}-{tone}-after.flac', samples, SAMPLE_RATE, subtype='PCM_16')
            support = render([n for n in score.notes if n.extra['role'] == 'accompaniment'],
                             result.duration_ms/1000+3, tone)
            sf.write(OUTPUT/'media'/f'{song}-{tone}-support.flac', support, SAMPLE_RATE, subtype='PCM_16')
            lead_audio, sr = sf.read(OUTPUT/'media'/f'{song}-{tone}-melody.flac', dtype='float32', always_2d=True)
            assert sr == SAMPLE_RATE
            for mode, current in (('before', before), ('after', score)):
                audit_row = dict(song=song, instrument=tone, mode=mode, melody_unchanged=True, **audit(current))
                if mode == 'after':
                    audit_row.update(peak=peak, clipped_samples=clipped)
                report['scores'].append(audit_row)
                for w in (w for w in report['windows'] if w['song'] == song):
                    row = dict(window=w['id'], instrument=tone, mode=mode,
                               melody=sum(w['start']*1000 <= n.time_ms < w['end']*1000
                                          for n in current.notes if n.extra['role'] == 'melody'),
                               **audit(current, w['start']*1000, w['end']*1000))
                    if mode == 'after':
                        a, b = round(w['start']*SAMPLE_RATE), round(w['end']*SAMPLE_RATE)
                        lead_power = float(np.mean(lead_audio[a:b]**2))
                        support_power = float(np.mean(support[a:b]**2))
                        row['support_to_lead_db'] = round(10*np.log10(max(support_power, 1e-12) /
                                                                    max(lead_power, 1e-12)), 2)
                    report['windows_audit'].append(row)
            print(song, tone, json.dumps(metrics), 'peak', round(peak, 3), flush=True)
            del samples, support, lead_audio
        print(song, '37 instruments passed', flush=True)
    (OUTPUT/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (OUTPUT/'index.html').write_text(HTML.replace('REPORT_DATA', json.dumps(report, ensure_ascii=False)
                                                .replace('</', '<\\/')))


HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>伴奏平衡 · 前后对照</title>
<style>
body{font:16px system-ui;background:#101820;color:#e8f0f6;max-width:1100px;margin:32px auto;padding:20px}
select,button{padding:10px;margin:5px;background:#233442;color:inherit;border:1px solid #627b8b;border-radius:7px}
audio{width:100%;margin:16px 0}td,th{padding:9px;text-align:left;border-bottom:1px solid #345}
.muted{color:#adbdca}#windows button{font-size:13px}h1{font-size:28px}table{font-size:14px}
</style><h1>主旋律＋和弦 · 伴奏平衡对照</h1>
<p>对比上一次密集版与本次平衡版。主旋律保持；伴奏控制音区、起音和强音尾叠响。
保留原始识别结果，统一音量 0.42、无混响，切换版本保留播放位置。</p>
<select id="song" aria-label="歌曲"></select>
<select id="tone" aria-label="音色"><option>Piano</option><option>Guitar</option><option>Violin</option><option>Flute</option></select>
<select id="mode" aria-label="版本"><option value="after">修改后 · 平衡版</option>
<option value="before">修改前 · 密集版</option><option value="melody">主旋律单听</option>
<option value="support">修改后 · 伴奏单听</option><option value="original">原曲参考</option></select>
<p id="range" class="muted"></p><audio id="player" controls></audio><div id="windows"></div>
<p id="detail"></p><div style="overflow:auto"><table id="metrics"></table></div>
<p class="muted">强音尾并发数依据采样包络估计。统计用于验证音区与叠响约束，听感请结合原曲和分声部对照。</p>
<script>
const data=REPORT_DATA;
const song=document.querySelector('#song'),tone=document.querySelector('#tone'),mode=document.querySelector('#mode'),
player=document.querySelector('#player');
data.cases.forEach(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.title;song.append(o)});
let revision=0;
function refresh(reset=false){
const current=++revision,t=reset?0:player.currentTime,playing=!player.paused;
player.pause();player.src='media/'+song.value+(mode.value==='original'?'-original':'-'+tone.value+'-'+mode.value)+'.flac';
player.onloadedmetadata=()=>{if(current!==revision)return;player.currentTime=Math.min(t,player.duration);
if(playing)player.play().catch(()=>{})};
const c=data.cases.find(c=>c.id===song.value);
document.querySelector('#range').textContent=(c.id==='love'?'90.83 秒片段':'完整可用录音')+' · '+c.seconds.toFixed(2)+' 秒';
const w=document.querySelector('#windows');w.replaceChildren();
data.windows.filter(x=>x.song===song.value).forEach(x=>{const b=document.createElement('button');
b.textContent=x.label+' · '+x.start+'–'+x.end+' s';b.onclick=()=>{player.currentTime=x.start;player.play()};
w.append(b)});
const rows=data.scores.filter(x=>x.song===song.value&&x.instrument===tone.value);
document.querySelector('#metrics').innerHTML='<tr><th>版本</th><th>伴奏音</th><th>伴奏起音组</th><th>侵入旋律音区</th><th>旋律期间最多强伴奏音尾</th></tr>'+
rows.map(x=>'<tr><td>'+(x.mode==='after'?'平衡版':'密集版')+'</td><td>'+x.accompaniment+
'</td><td>'+x.attacks+'</td><td>'+x.insufficient_lead_gap+'</td><td>'+x.maximum_strong_support_with_lead+'</td></tr>').join('');
document.querySelector('#detail').textContent='12 组音色对照主旋律逐音符保持；全部 37 种音色已检查。平衡版伴奏强音尾期间至少低于旋律 3 个半音。';
}
song.onchange=()=>refresh(true);tone.onchange=()=>refresh();mode.onchange=()=>refresh();refresh(true);
</script></html>
"""


if __name__ == '__main__':
    main()
