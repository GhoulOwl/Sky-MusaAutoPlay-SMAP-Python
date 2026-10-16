"""Compare fixed versus outward complete-note boundaries on source-pinned songs."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import soundfile as sf
from evaluate_refinement import ROOT, WINDOWS, recognize, summarize

from autoskyplay.audio import SAMPLE_RATE
from autoskyplay.netease import NeteaseSong
from autoskyplay.score import load_score
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.audition import render_window
from autoskyplay.transcription.drafts import DraftStore
from autoskyplay.transcription.events import EventResult
from autoskyplay.transcription.refinement import (
    CandidateStore,
    Region,
    frozen_rhythm,
    make_candidate,
    make_safe_candidate,
    search_context,
)


def audit(base, candidate, original, local):
    a, b = candidate.region.start, candidate.region.end
    def outside(score):
        return [asdict(n) for n in score.notes if not (a-1e-6 <= n.time_ms and n.time_ms+n.duration_ms <= b+1e-6)]
    assert outside(base) == outside(candidate.score)
    assert base.bpm == candidate.score.bpm
    assert base.root['transcription']['shift'] == candidate.score.root['transcription']['shift']
    # Every newly imported raw note keeps its full model interval, even when
    # the chosen seam moved. Old outside/crossing raw events also survive whole.
    lookup = {n.index:n for n in local.raw_notes}
    old = {n.identity:n for n in candidate.result.raw_notes if not n.identity.startswith('refine:'+candidate.ident+':')}
    for n in original.raw_notes:
        if not a <= n.start_ms <= n.start_ms+n.duration_ms <= b:
            kept = old[n.identity]
            assert (n.pitch,n.start_ms,n.duration_ms,n.instrument) == (kept.pitch,kept.start_ms,kept.duration_ms,kept.instrument)
    for n in candidate.result.raw_notes:
        if n.identity.startswith('refine:'+candidate.ident+':'):
            raw = lookup[int(n.identity.split(':')[-1])]
            assert n.duration_ms == raw.duration_ms and n.start_ms == raw.start_ms+candidate.region.context_start
    support = [n for n in candidate.score.notes if n.extra.get('role') == 'accompaniment']
    assert all(n.duration_ms >= 80-1e-6 for n in support)
    maximum = max((sum(m.time_ms <= n.time_ms < m.time_ms+m.duration_ms for m in support) for n in support), default=0)
    assert maximum <= (2 if base.root['transcription']['mode'] == 'chords' else 1)
    return dict(outside_exact=True, bpm_shift_fixed=True, raw_notes_whole=True, max_support_voices=maximum)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=ROOT/'artifacts/refinement-boundaries-2026-10-14')
    p.add_argument('--output', type=Path, default=ROOT/'artifacts/transcription-comparison-2026-10-14/refinement-safe-boundaries')
    p.add_argument('--data', type=Path, default=Path.home()/'Library/Application Support/AutoSkyPlay')
    args = p.parse_args()
    manifest = json.loads((args.root/'manifest.json').read_text())
    previous_root = ROOT/'artifacts/transcription-comparison-2026-10-14/refinement'
    previous = json.loads((previous_root/'comparison.json').read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    media = args.output/'media'
    media.mkdir(exist_ok=True)
    drafts = DraftStore(args.root/'review-data')
    store = CandidateStore(args.root/'review-data')
    report = dict(cases=manifest['cases'], comparisons=[], refinements=[], windows=[], render_audit=[], failures=[],
                  policy='outward-complete-events-v1', previous_blocked=sum(bool(r['conflicts']) for r in previous['refinements']))
    for case in manifest['cases']:
        song = case['id']
        assert hashlib.sha256(Path(case['source']).read_bytes()).hexdigest() == case['sha256']
        original = EventResult.load(case['events'])
        bases = {}
        for instrument, mode in [('Piano','melody'),('Piano','simple'),('Piano','chords'),('Violin','auto')]:
            score, _ = build_score(original, case['title'], instrument=instrument, mode=mode)
            key = instrument+'-'+mode
            bases[key] = score
            score.save(args.output/f'{song}-{key}.json')
            report['comparisons'].append(dict(song=song,profile=key,**summarize(score)))
        default, _ = build_score(original,case['title'],instrument='Piano')
        assert [(n.key,n.time_ms,n.duration_ms) for n in default.notes] == [(n.key,n.time_ms,n.duration_ms) for n in bases['Piano-chords'].notes]
        for wi,(a,b,label) in enumerate(WINDOWS[song]):
            requested = search_context(original, Region.create(original,a*1000,b*1000))
            versions = dict(bases)
            w = dict(id=f'{song}-{wi}',song=song,label=label,requested_start=a,requested_end=b,findings=[])
            actual = [(a,b)]
            for model in ('small','medium') if wi==1 else ('small',):
                try:
                    print('evaluate',song,wi,model,flush=True)
                    local = recognize(case,args.root/f'{song}-{wi}-{model}',args.data,requested.context_start/1000,
                                      (requested.context_end-requested.context_start)/1000,model,frozen_rhythm(original,requested))
                    local.export_midi(args.output/f'{song}-{wi}-{model}-local-raw.mid')
                    for instrument,mode in [('Piano','simple'),('Piano','chords'),('Violin','auto')]:
                        key = instrument+'-'+mode
                        base = bases[key]
                        params = dict(instrument=instrument,mode=mode,minimum=80,overrides=[],device='auto')
                        parent = drafts.save(None,NeteaseSong(**case['song']),original,base,params,source=Path(case['source']).parent)
                        fixed = make_candidate(parent,original,base,params,local,requested)
                        started = time.perf_counter()
                        c = make_safe_candidate(parent,original,base,params,local,requested)
                        elapsed = time.perf_counter()-started
                        checks = audit(base,c,original,local)
                        c.diagnostics.update(checks)
                        store.save(c,local)
                        child = store.accept(c) if not c.conflicts else None
                        old = next(r for r in previous['refinements'] if r['song']==song and r['window']==wi and r['model']==model and r['profile']==key)
                        info = dict(song=song,window=wi,model=model,profile=key,conflicts=c.conflicts,
                                    previous_conflicts=old['conflicts'],wide_fixed_conflicts=fixed.conflicts,
                                    requested=[a,b],actual=[c.region.start/1000,c.region.end/1000],
                                    context=[requested.context_start/1000,requested.context_end/1000],
                                    candidate_id=c.ident,child_id=child,diagnostics=c.diagnostics,
                                    seconds=local.metadata.get('evaluation_wall_seconds'),rss=local.metadata.get('sampled_child_rss_bytes'),
                                    mapping_seconds=elapsed,metrics=summarize(c.score))
                        report['refinements'].append(info)
                        c.score.save(args.output/f'{song}-{wi}-{model}-{key}-candidate.json')
                        versions[key+'-refine-'+model] = c.score
                        versions[key+'-previous-'+model] = load_score(previous_root/f'{song}-{wi}-{model}-{key}-candidate.json')
                        actual.append(info['actual'])
                        print('candidate',key,'fixed',len(fixed.conflicts),'safe',len(c.conflicts),'range',info['actual'],'map',round(elapsed,1),flush=True)
                except Exception as exc:
                    report['failures'].append(dict(song=song,window=wi,model=model,error=str(exc)))
                    print('FAILED',str(exc),flush=True)
            start = max(0,min(x[0] for x in actual)-1)
            end = min(original.duration_ms/1000,max(x[1] for x in actual)+1)
            w.update(start=start,end=end)
            w['versions'] = {}
            for version,score in versions.items():
                w['versions'][version] = [dict(t=n.time_ms,d=n.duration_ms,k=n.key,r=n.extra.get('role')) for n in score.notes
                                          if n.time_ms < end*1000 and n.time_ms+n.duration_ms > start*1000]
                for layer in ('full','lead'):
                    audio = render_window(score,start*1000,end*1000,version.split('-')[0],layer=='lead')
                    name = f'{w["id"]}-{version}-{layer}.wav'
                    sf.write(media/name,audio,SAMPLE_RATE,subtype='PCM_16')
                    report['render_audit'].append(dict(file=name,frames=len(audio),peak=float(np.max(abs(audio))),clipped=int(np.sum(abs(audio)>=.999))))
            with sf.SoundFile(case['source']) as stream:
                stream.seek(round(start*stream.samplerate))
                audio = stream.read(round((end-start)*stream.samplerate),dtype='float32',always_2d=True)
                sf.write(media/f'{w["id"]}-source.wav',audio*.5,stream.samplerate)
            report['windows'].append(w)
            (args.output/'comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (args.output/'invariants.json').write_text(json.dumps([dict(song=r['song'],window=r['window'],model=r['model'],profile=r['profile'],**r['diagnostics']) for r in report['refinements']],ensure_ascii=False,indent=2))
    template = Path(__file__).with_name('safe_boundary_review.html')
    if template.exists():
        (args.output/'index.html').write_text(template.read_text().replace('__DATA__',json.dumps(report,ensure_ascii=False).replace('</','<\\/')))
    review = ROOT/'docs/TRANSCRIPTION_SAFE_BOUNDARY_REVIEW_2026-10-14.md'
    if review.exists():
        # The narrative describes the dated reference run; comparison.json
        # contains the actual results of this invocation.
        content = review.read_text().replace(
            '../artifacts/transcription-comparison-2026-10-14/refinement-safe-boundaries/', '')
        (args.output/'report.md').write_text(content.replace(
            'TRANSCRIPTION_REFINEMENT_REVIEW_2026-10-14.md', '../refinement/report.md'))
    print('complete',len(report['refinements']),'blocked',sum(bool(r['conflicts']) for r in report['refinements']),'failures',report['failures'],flush=True)


if __name__ == '__main__':
    main()
