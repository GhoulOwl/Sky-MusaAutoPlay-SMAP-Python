"""Real-song, source-pinned chord and local refinement comparisons (no new beat model)."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from autoskyplay.audio import SAMPLE_RATE
from autoskyplay.netease import NeteaseAudio, NeteaseSong
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.audition import render_window
from autoskyplay.transcription.drafts import DraftStore
from autoskyplay.transcription.engine import Engine
from autoskyplay.transcription.events import EventResult
from autoskyplay.transcription.refinement import CandidateStore, Region, frozen_rhythm, make_candidate
from autoskyplay.transcription.task import TranscriptionTask

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = {
    'summer': [(0, 16, '前奏'), (30, 42, '主题'), (80, 92, '器乐发展')],
    'secret': [(0, 16, '前奏'), (60, 72, '人声乐句'), (200, 212, '后段乐句')],
    'love': [(0, 16, '前奏'), (29, 36, '长音与音高'), (65, 77, '旋律与伴奏')],
}


def recognize(case, root, data, start, length, model='small', rhythm=None):
    root.mkdir(parents=True, exist_ok=True)
    cache = root/'events.json'
    if cache.exists():
        return EventResult.load(cache)
    engine = Engine([str(ROOT/'build/muscriptor-venv/bin/python'), str(ROOT/'src/autoskyplay/transcription/muscriptor_worker.py')],
                    data/'transcription/models', '2.0.0-refine-v1', 'muscriptor', model, 'auto')
    task = TranscriptionTask(root, SimpleNamespace(), NeteaseSong(**case['song']),
                            NeteaseAudio(case['song']['id'], '', case['source_duration']*1000),
                            engine, start, length, source=Path(case['source']), source_kind='local_audio', rhythm_reference=rhythm)
    beginning, peak = time.perf_counter(), 0
    try:
        task.start()
        while task.running:
            if task.process and task.process.poll() is None:
                output = subprocess.run(['ps', '-o', 'rss=', '-p', str(task.process.pid)], capture_output=True, text=True).stdout.strip()
                if output.isdigit():
                    peak = max(peak, int(output)*1024)
            time.sleep(.2)
        events = list(task.events.queue)
        if task.result is None:
            raise ValueError(next((e['message'] for e in reversed(events) if e['stage'] == 'error'), '识别失败'))
        task.result.metadata['evaluation_wall_seconds'] = time.perf_counter()-beginning
        task.result.metadata['sampled_child_rss_bytes'] = peak
        task.result.save(cache)
        return task.result
    finally:
        if (task.work/'engine.stderr').exists():
            (root/'engine.stderr').write_bytes((task.work/'engine.stderr').read_bytes())
        task.close()


def summarize(score):
    lead = [n for n in score.notes if n.extra.get('role') != 'accompaniment']
    support = [n for n in score.notes if n.extra.get('role') == 'accompaniment']
    counts = {}
    for n in support:
        counts[n.time_ms] = counts.get(n.time_ms, 0)+1
    return dict(melody=len(lead), support=len(support), dyads=sum(c == 2 for c in counts.values()),
                long_notes=sum(n.duration_ms >= 1000 for n in lead),
                tiny_support=sum(n.duration_ms < 80-1e-6 for n in support),
                same_key_pairs=sum(a.key == b.key and -1 <= b.time_ms-a.time_ms-a.duration_ms <= 80
                                   for a, b in zip(lead, lead[1:])),
                uncertain_seconds=sum(s['end_ms']-s['start_ms'] for s in score.root['transcription']['sections']
                                      if s['source'] == 'uncertain')/1000,
                clarity=score.root['transcription'].get('clarity', {}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT/'artifacts/refinement-2026-10-14')
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts/transcription-comparison-2026-10-14/refinement')
    parser.add_argument('--data', type=Path, default=Path.home()/'Library/Application Support/AutoSkyPlay')
    args = parser.parse_args()
    manifest = json.loads((args.root/'manifest.json').read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    media = args.output/'media'
    media.mkdir(exist_ok=True)
    store = CandidateStore(args.root/'review-data')
    drafts = DraftStore(args.root/'review-data')
    report = dict(cases=manifest['cases'], comparisons=[], refinements=[], render_audit=[], failures=[])
    for case in manifest['cases']:
        song = case['id']
        source = Path(case['source'])
        assert hashlib.sha256(source.read_bytes()).hexdigest() == case['sha256']
        # Summer/secret reuse frozen native results; love has a 90.83 s historical
        # baseline and is explicitly kept at that range for reproducible comparison.
        original = EventResult.load(case['events'])
        baseline_scores = {}
        for instrument, mode in [('Piano', 'melody'), ('Piano', 'simple'), ('Piano', 'chords'), ('Violin', 'auto')]:
            score, _ = build_score(original, case['title'], instrument=instrument, mode=mode)
            key = instrument+'-'+mode
            baseline_scores[key] = score
            score.save(args.output/f'{song}-{key}.json')
            report['comparisons'].append(dict(song=song, profile=key, **summarize(score)))
        def lead(s):
            return [(n.key, n.time_ms, n.duration_ms) for n in s.notes if n.extra.get('role') != 'accompaniment']
        assert lead(baseline_scores['Piano-simple']) == lead(baseline_scores['Piano-chords'])
        original.export_midi(args.output/f'{song}-baseline-raw.mid')
        for wi, (a, b, label) in enumerate(WINDOWS[song]):
            region = Region.create(original, a*1000, b*1000)
            w = dict(id=f'{song}-{wi}', song=song, label=label, start=a, end=b, versions={}, findings=[])
            for name, score in baseline_scores.items():
                w['versions'][name] = score
            for model in ('small', 'medium') if wi == 1 else ('small',):
                print('recognize', song, wi, model, flush=True)
                try:
                    local = recognize(case, args.root/f'{song}-{wi}-{model}', args.data,
                                      region.context_start/1000, (region.context_end-region.context_start)/1000,
                                      model, frozen_rhythm(original, region))
                    local.export_midi(args.output/f'{song}-{wi}-{model}-local-raw.mid')
                    for instrument, mode in [('Piano', 'simple'), ('Piano', 'chords'), ('Violin', 'auto')]:
                        params = dict(instrument=instrument, mode=mode, minimum=80, overrides=[], device='auto')
                        base = baseline_scores[instrument+'-'+mode]
                        parent = drafts.save(None, NeteaseSong(**case['song']), original, base, params,
                                             source=source.parent)
                        c = make_candidate(parent, original, base, params, local, region)
                        def protected(s):
                            return [asdict(n) for n in s.notes if not (
                                region.start-1e-6 <= n.time_ms and n.time_ms+n.duration_ms <= region.end+1e-6)]
                        assert protected(base) == protected(c.score)
                        assert c.score.bpm == base.bpm and c.score.root['transcription']['shift'] == base.root['transcription']['shift']
                        supports = [n for n in c.score.notes if n.extra.get('role') == 'accompaniment']
                        assert all(n.duration_ms >= 80-1e-6 for n in supports)
                        maximum = max((sum(m.time_ms <= n.time_ms < m.time_ms+m.duration_ms for m in supports)
                                       for n in supports), default=0)
                        assert maximum <= (2 if mode == 'chords' else 1)
                        c.diagnostics.update(outside_exact=True, bpm_shift_fixed=True, max_support_voices=maximum)
                        store.save(c, local)
                        # Exercise both branches. Never publish a conflicting patch as accepted.
                        child = store.accept(c) if not c.conflicts else None
                        c.score.save(args.output/f'{song}-{wi}-{model}-{instrument}-{mode}-candidate.json')
                        info = dict(song=song, window=wi, model=model, profile=instrument+'-'+mode,
                                    conflicts=c.conflicts, candidate_id=c.ident, child_id=child,
                                    diagnostics=c.diagnostics, seconds=local.metadata.get('evaluation_wall_seconds'),
                                    rss=local.metadata.get('sampled_child_rss_bytes'),
                                    rhythm_reference=local.metadata.get('rhythm_reference'), metrics=summarize(c.score))
                        report['refinements'].append(info)
                        w['versions'][f'{instrument}-{mode}-refine-{model}'] = c.score
                        if c.conflicts:
                            w['findings'].append(f'{instrument}/{mode}/{model}：边界冲突，禁止接受')
                except Exception as exc:
                    report['failures'].append(dict(song=song, window=wi, model=model, error=str(exc)))
                    w['findings'].append(f'{model} 失败：{exc}')
            serial = {}
            for version, score in w['versions'].items():
                instrument = version.split('-')[0]
                serial[version] = [dict(t=n.time_ms, d=n.duration_ms, k=n.key, r=n.extra.get('role')) for n in score.notes
                                   if n.time_ms < b*1000 and n.time_ms+n.duration_ms > a*1000]
                for layer in ('full', 'lead'):
                    audio = render_window(score, a*1000, b*1000, instrument, layer == 'lead')
                    name = f'{w["id"]}-{version}-{layer}.wav'
                    sf.write(media/name, audio, SAMPLE_RATE, subtype='PCM_16')
                    report['render_audit'].append(dict(file=name, frames=len(audio), peak=float(np.max(abs(audio))),
                                                      clipped=int(np.sum(abs(audio) >= .999))))
            w['versions'] = serial
            with sf.SoundFile(source) as stream:
                stream.seek(round(a*stream.samplerate))
                audio = stream.read(round((b-a)*stream.samplerate), dtype='float32', always_2d=True)
                sf.write(media/f'{w["id"]}-source.wav', audio*.5, stream.samplerate)
            report.setdefault('windows', []).append(w)
            (args.output/'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print('finished', song, wi, flush=True)
    template = Path(__file__).with_name('refinement_review.html')
    (args.output/'invariants.json').write_text(json.dumps([
        dict(song=r['song'], window=r['window'], model=r['model'], profile=r['profile'],
             accepted=bool(r['child_id']), **r['diagnostics']) for r in report['refinements']
    ], ensure_ascii=False, indent=2))
    if template.exists():
        (args.output/'index.html').write_text(template.read_text().replace('__DATA__', json.dumps(report, ensure_ascii=False).replace('</', '<\\/')))
    review = ROOT/'docs/TRANSCRIPTION_REFINEMENT_REVIEW_2026-10-14.md'
    if review.exists():
        (args.output/'report.md').write_text(review.read_text())
    print('done', len(report['refinements']), 'failures', len(report['failures']), flush=True)


if __name__ == '__main__':
    main()
