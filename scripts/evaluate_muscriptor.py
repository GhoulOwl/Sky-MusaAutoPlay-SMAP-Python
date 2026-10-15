"""Reproduce old/small/medium Sky scores and an offline listening comparison."""
from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path

import numpy as np
import soundfile as sf

from autoskyplay.audio import SAMPLE_RATE, Mixer
from autoskyplay.score import load_score
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.events import EventResult


def render(notes, duration, instrument):
    mixer = Mixer()
    mixer.prepare(instrument)
    mixer.volume = .42
    output = np.zeros((round(duration*SAMPLE_RATE), 2), np.float32)
    cursor = 0

    def advance(target):
        nonlocal cursor
        while cursor < target:
            count = min(8192, target-cursor)
            output[cursor:cursor+count] = mixer.render(count)
            cursor += count

    for note in sorted(notes, key=lambda n: (n.time_ms, n.key)):
        frame = round(note.time_ms/1000*SAMPLE_RATE)
        if frame >= len(output):
            break
        advance(frame)
        mixer.trigger((note.key,), (note.duration_ms,))
    advance(len(output))
    return output


def metrics(score, duration):
    lead = [n for n in score.notes if n.extra.get('role') != 'accompaniment']
    support = [n for n in score.notes if n.extra.get('role') == 'accompaniment']
    pairs = [dict(time_ms=b.time_ms, gap_ms=b.time_ms-a.time_ms-a.duration_ms,
                  raw_same=a.extra['sourcePitch'] == b.extra['sourcePitch'])
             for a, b in zip(lead, lead[1:]) if a.key == b.key and -1 <= b.time_ms-a.time_ms-a.duration_ms <= 80]
    meta = score.root['transcription']
    return dict(melody=len(lead), accompaniment=len(support),
                long_notes=sum(n.duration_ms >= 1000 for n in lead),
                same_key_adjacent=len(pairs), same_key_pairs=pairs,
                tiny_accompaniment=sum(n.duration_ms < 80-1e-6 for n in support),
                uncertain_seconds=sum(s['end_ms']-s['start_ms'] for s in meta['sections'] if s['needs_review'])/1000,
                melody_seconds=sum(n.duration_ms for n in lead)/1000,
                mapping_review=len(meta.get('mapping_review', [])), duration=duration)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw', type=Path, default=Path('artifacts/muscriptor-2026-10-14'))
    parser.add_argument('--baseline', type=Path, default=Path('artifacts/transcription-comparison-2026-10-14/sustain'))
    parser.add_argument('--output', type=Path, default=Path('artifacts/transcription-comparison-2026-10-14/muscriptor'))
    parser.add_argument('--skip-audio', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'media').mkdir(exist_ok=True)
    cases = json.loads((args.raw/'manifest.json').read_text())['cases']
    windows = json.loads((args.baseline/'windows.json').read_text())
    for w in windows:
        w.pop('finding', None)
        w['label'] = w['label'].replace('延音切分修复', '延音切分重点')
    data = dict(cases=cases, windows=windows, scores={}, metrics=[], raw_diagnostics=[], render_audit=[])
    if args.skip_audio and (args.output/'comparison.json').exists():
        data['render_audit'] = json.loads((args.output/'comparison.json').read_text()).get('render_audit', [])
    for case in cases:
        song = case['id']
        for version in ('old', 'small', 'medium'):
            result = None if version == 'old' else EventResult.load(args.raw/f'{version}-{song}'/'events.json')
            if result:
                data['raw_diagnostics'].append(dict(song=song, model=version, count=len(result.raw_notes),
                    instruments=dict(Counter(n.instrument for n in result.raw_notes)), metadata=result.metadata,
                    long_notes=sum(n.duration_ms >= 1000 for n in result.raw_notes),
                    crossing_5s=sum(int(n.start_ms//5000) != int((n.start_ms+n.duration_ms-1)//5000)
                                    for n in result.raw_notes)))
            for instrument, mode in (('Piano','auto'), ('Violin','auto'), ('Violin','simple')):
                profile = instrument + ('-optional' if mode == 'simple' else '')
                if result:
                    score, _ = build_score(result, case['title'], instrument=instrument, mode=mode)
                else:
                    variant = instrument + ('-accompaniment' if mode == 'simple' else '')
                    score = load_score(args.baseline/song/f'after-{variant}-score.json')
                score.save(args.output/f'{song}-{version}-{profile}.json')
                if result and profile == 'Piano':
                    result.export_midi(args.output/f'{song}-{version}-raw.mid')
                entry = dict(song=song, version=version, profile=profile, **metrics(score,case['length']))
                data['metrics'].append(entry)
                data['scores'][f'{song}-{version}-{profile}'] = dict(
                    sections=score.root['transcription']['sections'],
                    notes=[dict(t=n.time_ms,d=n.duration_ms,k=n.key,r=n.extra.get('role','melody'),
                                source=n.extra.get('modelInstrument',n.extra.get('source','')),
                                p=n.extra.get('sourcePitch')) for n in score.notes])
                if not args.skip_audio:
                    for layer in ('full', 'lead'):
                        notes = score.notes if layer == 'full' else [n for n in score.notes if n.extra.get('role') != 'accompaniment']
                        output = render(notes, case['length'], instrument)
                        for w in (w for w in windows if w['song'] == song):
                            clip = output[round(w['start']*SAMPLE_RATE):round(w['end']*SAMPLE_RATE)]
                            name = f'{w["id"]}-{version}-{profile}-{layer}.wav'
                            sf.write(args.output/'media'/name, clip, SAMPLE_RATE, subtype='PCM_16')
                            data['render_audit'].append(dict(file=name, frames=len(clip), peak=float(np.max(abs(clip))),
                                                            clipped_samples=int(np.sum(abs(clip) >= .999))))
                print(song,version,profile,entry['melody'],entry['accompaniment'],flush=True)
    (args.output/'comparison.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
    (args.output/'manifest.json').write_text((args.raw/'manifest.json').read_text())
    template = Path(__file__).with_name('muscriptor_review.html').read_text()
    (args.output/'index.html').write_text(template.replace('__COMPARISON_DATA__',json.dumps(data,ensure_ascii=False).replace('</','<\\/')))
    report = Path('docs/TRANSCRIPTION_MUSCRIPTOR_REVIEW_2026-10-14.md')
    if report.exists():
        shutil.copy2(report,args.output/'report.md')


if __name__ == '__main__':
    main()
