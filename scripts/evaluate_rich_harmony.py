"""Reproducible full-recording comparison; no recognition/model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf
from evaluate_muscriptor import render

from autoskyplay.audio import SAMPLE_RATE
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.events import EventResult


def lead_identity(score):
    return [(n.key, n.time_ms, n.duration_ms, n.extra['sourcePitch'])
            for n in score.notes if n.extra['role'] == 'melody']


def audit(score):
    notes = score.notes
    maximum, duplicate = 0, 0
    for t in sorted({n.time_ms for n in notes}):
        active = [n for n in notes if n.time_ms <= t < n.time_ms+n.duration_ms]
        maximum = max(maximum, len(active))
        duplicate += len(active)-len({n.key for n in active})
    return dict(notes=len(notes), accompaniment=sum(n.extra['role'] == 'accompaniment' for n in notes),
                generated=sum(n.extra.get('generated', False) for n in notes),
                maximum_polyphony=maximum, same_key_conflicts=duplicate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('artifacts/rich-harmony-2026-10-20'))
    args = parser.parse_args()
    out = args.output
    (out/'media').mkdir(parents=True, exist_ok=True)
    base = Path('artifacts/hybrid-2026-10-14')
    cases = json.loads((base/'manifest.json').read_text())['cases']
    comparison = json.loads(Path('artifacts/transcription-comparison-2026-10-14/hybrid/comparison.json').read_text())
    windows = comparison['windows']
    report = dict(volume=.42, reverb=False, cases=[], scores=[], windows=windows, window_metrics=[])
    for case in cases:
        song = case['id']
        raw_file = base/f'full-{song}'/'events.json'
        result = EventResult.load(raw_file)
        report['cases'].append(dict(id=song, title=case['title'], seconds=result.duration_ms/1000,
                                    range=case['evaluated_range'],
                                    events_sha256=hashlib.sha256(raw_file.read_bytes()).hexdigest()))
        original, sr = sf.read(base/f'full-{song}'/'original.wav', dtype='float32', always_2d=True)
        sf.write(out/'media'/f'{song}-original.flac', original, sr, subtype='PCM_16')
        del original
        for instrument in ('Piano', 'Guitar', 'Violin', 'Flute'):
            reference, _ = build_score(result, case['title'], instrument=instrument, mode='melody')
            for mode in ('auto', 'rich_chords', 'melody'):
                score, _ = build_score(result, case['title'], instrument=instrument, mode=mode)
                ident = f'{song}-{instrument}-{mode}'
                score.save(out/f'{ident}.json')
                metrics = audit(score)
                metrics.update(id=ident, song=song, instrument=instrument, mode=mode,
                               melody_unchanged=lead_identity(score) == lead_identity(reference),
                               clarity=score.root['transcription']['clarity'])
                assert metrics['melody_unchanged']
                if mode == 'rich_chords':
                    assert metrics['maximum_polyphony'] <= 4 and metrics['same_key_conflicts'] == 0
                output = render(score.notes, result.duration_ms/1000+3, instrument)
                metrics.update(peak=float(np.max(np.abs(output))),
                               clipped_samples=int(np.sum(np.abs(output) >= 1)))
                sf.write(out/'media'/f'{ident}.flac', output, SAMPLE_RATE, subtype='PCM_16')
                report['scores'].append(metrics)
                for window in (w for w in windows if w['song'] == song):
                    a, b = window['start']*1000, window['end']*1000
                    notes = [n for n in score.notes if n.time_ms < b and n.time_ms+n.duration_ms > a]
                    report['window_metrics'].append(dict(id=window['id'], profile=ident, notes=len(notes),
                        accompaniment=sum(n.extra['role'] == 'accompaniment' for n in notes)))
                print(ident, json.dumps({k: v for k, v in metrics.items() if k != 'clarity'}), flush=True)
                del output
    (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    page = HTML.replace('REPORT_DATA', json.dumps(report, ensure_ascii=False).replace('</', '<\\/'))
    (out/'index.html').write_text(page, encoding='utf-8')


HTML = """<!doctype html><meta charset="utf-8"><title>整曲和弦对照</title>
<style>body{font:16px system-ui;background:#101820;color:#e8f0f6;max-width:1100px;margin:40px auto;padding:20px}
select,button{padding:10px;margin:6px;background:#233442;color:inherit;border:1px solid #627b8b;border-radius:7px}
audio{width:100%;margin:12px 0}td,th{padding:9px;text-align:left;border-bottom:1px solid #345}
.muted{color:#adbdca}#windows button{font-size:13px}h1{font-size:28px}</style>
<h1>主旋律＋和弦 · 整曲对照</h1>
<p>统一采样、音量 0.42、无混响；主旋律逐音符保持一致。新模式依据原曲证据补全，
允许前奏、间奏、尾奏独立伴奏。音符数量增加不等于识别更准确，听感仍需核对。</p>
<select id="song"></select><select id="tone"><option>Piano</option><option>Guitar</option><option>Violin</option><option>Flute</option></select>
<select id="mode"><option value="rich_chords">新：主旋律＋和弦</option><option value="auto">旧：按音色推荐</option>
<option value="melody">仅主旋律</option><option value="original">原曲参考</option></select>
<p id="range" class="muted"></p><audio id="player" controls></audio><div id="windows"></div>
<p id="details"></p><table id="metrics"></table>
<script>
const data=REPORT_DATA;
const names={auto:'旧推荐模式',rich_chords:'主旋律＋和弦',melody:'仅主旋律'};
const song=document.querySelector('#song'),tone=document.querySelector('#tone'),mode=document.querySelector('#mode'),
player=document.querySelector('#player');
data.cases.forEach(c=>{const o=document.createElement('option');o.value=c.id;o.textContent=c.title;song.append(o)});
function refresh(reset=false){
const t=reset?0:player.currentTime,playing=!player.paused;
player.src='media/'+song.value+(mode.value==='original'?'-original':'-'+tone.value+'-'+mode.value)+'.flac';
player.onloadedmetadata=()=>{player.currentTime=Math.min(t,player.duration);if(playing)player.play()};
const current=data.cases.find(c=>c.id===song.value);
document.querySelector('#range').textContent=(current.id==='love'?'已验证片段':'完整可用录音')+' · '+current.seconds.toFixed(2)+' 秒';
const w=document.querySelector('#windows');w.replaceChildren();
data.windows.filter(x=>x.song===song.value).forEach(x=>{const b=document.createElement('button');
b.textContent=x.label+' · '+x.start+'–'+x.end+' s';b.onclick=()=>{player.currentTime=x.start;player.play()};w.append(b)});
const rows=data.scores.filter(x=>x.song===song.value&&x.instrument===tone.value);
document.querySelector('#metrics').innerHTML='<tr><th>模式</th><th>伴奏音</th><th>补全音</th><th>最大同时键</th><th>峰值</th><th>削波采样</th></tr>'+
rows.map(x=>'<tr><td>'+names[x.mode]+'</td><td>'+x.accompaniment+'</td><td>'+x.generated+'</td><td>'+x.maximum_polyphony+'</td><td>'+x.peak.toFixed(3)+'</td><td>'+x.clipped_samples+'</td></tr>').join('');
const selected=rows.find(x=>x.mode===mode.value);
document.querySelector('#details').textContent=selected?'主旋律保持：'+(selected.melody_unchanged?'已通过':'未通过')+'；同键冲突：'+selected.same_key_conflicts:'原曲仅供参考，不与合成谱逐文件归一化。';
}
song.onchange=()=>refresh(true);tone.onchange=()=>refresh();mode.onchange=()=>refresh();refresh(true);
</script>"""


if __name__ == '__main__':
    main()
