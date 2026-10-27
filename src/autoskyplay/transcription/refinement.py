"""Transactional local re-recognition. Context is evidence, never edit permission."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ..score import Score, atomic_json, parse_score
from .arrangement import build_score
from .drafts import DraftStore
from .events import EventResult
from .performance import piano_tails


def fingerprint(result, score, params):
    def canonical(value):
        if isinstance(value, dict):
            return {k: canonical(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [canonical(v) for v in value]
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    payload = dict(notes=[asdict(n) for n in result.raw_notes], metadata=result.metadata,
                   analysis=result.analysis, duration=result.duration_ms, bpm=result.bpm,
                   score=score.to_data(), params=params)
    return hashlib.sha256(json.dumps(canonical(payload), sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def eligible(result):
    return (isinstance(result, EventResult) and result.metadata.get('backend') != 'hybrid'
            and all(n.provider == 'muscriptor' and n.audio_source == 'original' for n in result.raw_notes))


def source_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def note_diff(before, after):
    """Exact performance differences, not an accuracy score or pitch matching claim."""
    from collections import Counter
    def keys(score):
        return Counter((n.key, n.time_ms, n.duration_ms, n.extra.get('role')) for n in score.notes)
    old, new = keys(before), keys(after)
    return dict(removed=sum((old-new).values()), added=sum((new-old).values()),
                identical=sum((old & new).values()))


@dataclass(frozen=True)
class Region:
    start: float
    end: float
    context_start: float
    context_end: float

    @classmethod
    def create(cls, result, start, end, beats_per_group=4):
        if not eligible(result):
            raise ValueError('首版局部精修仅支持 MuScriptor 原混音草稿')
        if not all(math.isfinite(t) for t in (start, end)) or not 0 <= start < end <= result.duration_ms:
            raise ValueError('请选择当前草稿内的精修范围')
        if end-start > 30000:
            raise ValueError('一次精修最多 30 秒，请缩小选区')
        beats = result.analysis.get('beats_ms', [])
        period = sorted(b-a for a, b in zip(beats, beats[1:]))
        pulse = period[len(period)//2] if period else 60000/result.bpm
        context = min(8000, max(2000, pulse*beats_per_group))
        return cls(start, end, max(0, start-context), min(result.duration_ms, end+context))


def reference_groups(result, count=4, origin=0):
    """Detected pulses grouped for selection; no implied downbeat or meter."""
    if not 2 <= count <= 12:
        raise ValueError('参考分组须为 2–12 拍')
    if result.analysis.get('beat_confidence', 0) < .55:
        return []
    beats = result.analysis.get('beats_ms', [])
    if len(beats) < 4:
        return []
    anchor = min(range(len(beats)), key=lambda i: abs(beats[i]-origin))
    return [b for i, b in enumerate(beats) if (i-anchor) % count == 0]


def frozen_rhythm(result, region):
    return dict(bpm=result.bpm, bpm_estimated=result.metadata.get('bpm_estimated', True),
                beat_confidence=result.analysis.get('beat_confidence', 0),
                beats_ms=[b-region.context_start for b in result.analysis.get('beats_ms', [])
                          if region.context_start <= b <= region.context_end])


def search_context(result, region):
    """One recognition with room for a bounded outward seam search and lead-in."""
    Region.create(result, region.start, region.end)
    return replace(region, context_start=max(0, region.start-8000),
                   context_end=min(result.duration_ms, region.end+8000))


def safe_regions(original, baseline, local, requested, expansion=4000):
    """Rank complete-note seams; never shrink the requested musical passage.

    Use all raw voices (including percussion) and the edited performance. A
    crossing model event must correspond to exactly one old event, one-to-one;
    a nearby same-key attack alone is not enough. The final assembler still
    checks quantization and protected sustained voices independently.
    """
    original.validate()
    local.validate()
    if not 0 <= expansion <= 4000:
        raise ValueError('安全边界向外搜索最多 4 秒')
    local_notes = [replace(n, start_ms=n.start_ms+requested.context_start) for n in local.raw_notes]
    raw = original.raw_notes
    performance = [(n.time_ms, n.time_ms+n.duration_ms) for n in baseline.notes]
    intervals = [(n.start_ms, n.start_ms+n.duration_ms) for n in raw+local_notes]+performance
    edges = sorted({t for a, b in intervals for t in (a, b)})
    points = {requested.start, requested.end, 0., original.duration_ms, *edges}
    # Mid-gap seams tolerate small alignment differences without moving a note.
    points.update((a+b)/2 for a, b in zip(edges, edges[1:]) if b-a >= 20)
    def candidates(lo, hi, target):
        choices = []
        for t in sorted(p for p in points if lo <= p <= hi):
            old = [n for n in raw if n.start_ms < t < n.start_ms+n.duration_ms]
            new = [n for n in local_notes if n.start_ms < t < n.start_ms+n.duration_ms]
            matches = [[p.index for p in old if _same_event(n, p)] for n in new]
            if any(len(m) != 1 for m in matches) or len({m[0] for m in matches}) != len(matches):
                continue
            crossing = sum(a < t < b for a, b in performance)
            distance = abs(t-target)
            # Prefer a break in the played lead/support, then a nearby seam.
            choices.append((crossing, distance, len(old)-len(matches), t))
        return sorted(choices)
    # At least one second of context remains after expansion (except file ends).
    lo = max(0, requested.start-expansion, requested.context_start+1000 if requested.context_start else 0)
    hi = min(original.duration_ms, requested.end+expansion,
             requested.context_end-1000 if requested.context_end < original.duration_ms else original.duration_ms)
    left = candidates(lo, requested.start, requested.start)
    right = candidates(requested.end, hi, requested.end)
    pairs = []
    # Event-level matching is cheap; cap the Cartesian search, not the evidence.
    def bounded(choices, target):
        return choices[:64]+[c for c in choices[64:] if c[3] == target]
    for a in bounded(left, requested.start):
        for b in bounded(right, requested.end):
            if 0 < b[3]-a[3] <= 30000:
                pairs.append((a[0]+b[0], a[1]+b[1], a[2]+b[2], a[3], b[3]))
    return [replace(requested, start=a, end=b) for *_, a, b in sorted(pairs)]


def make_safe_candidate(parent_id, original, baseline, params, local, requested, cancelled=lambda: False):
    """Bounded selection, followed by the same strict assembler as manual mode."""
    regions = safe_regions(original, baseline, local, requested)
    attempts, selected = [], None
    # Preserve an already safe exact request rather than widening it gratuitously.
    exact = next((r for r in regions if r.start == requested.start and r.end == requested.end), None)
    if exact:
        regions.remove(exact)
        regions.insert(0, exact)
    avoid_left, avoid_right = [], []
    for region in regions:
        if len(attempts) >= 6:
            break
        # A failed protected hold supplies a useful bound. Retrying the same
        # overlap a few milliseconds away cannot resolve that particular seam.
        if any(a < region.start < b for a, b in avoid_left) or any(a < region.end < b for a, b in avoid_right):
            continue
        if cancelled():
            raise ValueError('精修已取消，原草稿未改变')
        c = make_candidate(parent_id, original, baseline, params, local, region)
        attempts.append(dict(range=[region.start, region.end], conflicts=len(c.conflicts)))
        if not c.conflicts:
            selected = c
            break
        for collision in c.diagnostics['protected_collisions']:
            a, b = collision['protected_interval']
            if a < region.start < b:
                avoid_left.append((a, b))
            if a < region.end < b:
                avoid_right.append((a, b))
    if selected is None:
        if cancelled():
            raise ValueError('精修已取消，原草稿未改变')
        selected = make_candidate(parent_id, original, baseline, params, local, requested)
    decision = dict(policy='outward-complete-events-v1', requested=[requested.start, requested.end],
                    actual=[selected.region.start, selected.region.end], expansion_limit_ms=4000,
                    attempts=attempts, safe=not selected.conflicts, candidate_regions=len(regions))
    selected.diagnostics['boundary_search'] = decision
    selected.result.metadata['refinement']['boundary_search'] = decision
    # Score and raw result must persist the same selection evidence after reload.
    selected.score.root['transcription']['refinement']['boundary_search'] = decision
    return selected


def _inside(n, start, end):
    return n.time_ms >= start-1e-6 and n.time_ms+n.duration_ms <= end+1e-6


def _overlap(a, b, c, d):
    return max(0, min(b, d)-max(a, c))


def _same_event(a, b):
    overlap = _overlap(a.start_ms, a.start_ms+a.duration_ms, b.start_ms, b.start_ms+b.duration_ms)
    return (a.pitch == b.pitch and a.instrument == b.instrument and
            abs(a.start_ms-b.start_ms) <= 120 and overlap/max(a.duration_ms, b.duration_ms) >= .7)


def _sections(old, new, start, end):
    spans = []
    for source, regions in ((False, old), (True, new)):
        for s in regions:
            a, b = s['start_ms'], s['end_ms']
            bounds = [(max(a, start), min(b, end))] if source else [(a, min(b, start)), (max(a, end), b)]
            spans.extend(dict(s, start_ms=x, end_ms=y) for x, y in bounds if y > x)
    return sorted(spans, key=lambda s: s['start_ms'])


@dataclass
class Candidate:
    ident: str
    parent_id: str
    baseline: str
    region: Region
    result: EventResult
    score: Score
    conflicts: list[str]
    diagnostics: dict


def make_candidate(parent_id, original, baseline, params, local, region):
    if not eligible(original) or not eligible(local):
        raise ValueError('候选来源不兼容')
    original.validate()
    local.validate()
    if not 0 <= region.context_start <= region.start < region.end <= region.context_end <= original.duration_ms:
        raise ValueError('精修选区或上下文无效')
    if region.end-region.start > 30000 or abs(local.duration_ms-(region.context_end-region.context_start)) > 100:
        raise ValueError('候选范围与当前选区不匹配，请重新生成')
    expected = original.metadata.get('source_sha256')
    actual = local.metadata.get('source_sha256')
    if expected and actual and expected != actual:
        raise ValueError('候选与原稿使用了不同的音频文件')
    if 'source_start' in local.metadata and abs(local.metadata['source_start']-
            original.metadata.get('source_start', 0)-region.context_start/1000) > .05:
        raise ValueError('候选音频入点不匹配')
    ident = uuid.uuid4().hex
    left, right = region.start, region.end
    protected = [n for n in original.raw_notes
                 if n.start_ms < left < n.start_ms+n.duration_ms or n.start_ms < right < n.start_ms+n.duration_ms]
    retained = [n for n in original.raw_notes if not (left <= n.start_ms and n.start_ms+n.duration_ms <= right)]
    added, conflicts, matched = [], [], set()
    for n in local.raw_notes:
        n = replace(n, start_ms=n.start_ms+region.context_start,
                    event_id=f'refine:{ident}:{n.index}')
        end = n.start_ms+n.duration_ms
        if end <= left or n.start_ms >= right:
            continue
        matches = [p for p in protected if _same_event(n, p)]
        if matches:
            if len(matches) != 1 or matches[0].identity in matched:
                conflicts.append(f'{n.start_ms/1000:.2f}s 的同音事件无法一一对应，保留重奏边界并禁止接受')
            else:
                matched.add(matches[0].identity)
            continue
        if n.start_ms < left or end > right:
            conflicts.append(f'{n.start_ms/1000:.2f}s 的 {n.instrument} 候选跨过选区边界，请扩大范围')
            continue
        added.append(n)
    merged = copy.deepcopy(original)
    merged.raw_notes = [replace(n, index=i, event_id=n.identity) for i, n in enumerate(retained+added)]
    added_indices = {n.index for n in merged.raw_notes if n.event_id.startswith(f'refine:{ident}:')}
    merged.metadata.update(refinement=dict(id=ident, parent=parent_id, range=[left, right],
        context=[region.context_start, region.context_end], model=local.metadata.get('model'),
        model_sha256=local.metadata.get('model_sha256'), raw_kind='composite-original-and-local-events'),
        warnings=[*original.metadata.get('warnings', []), '当前识别事件由原版与局部精修组成；原始候选另行保留。'])
    info = baseline.root.get('transcription', {})
    anchors = {(n.extra.get('sourceTime', n.time_ms), n.extra.get('sourcePitch'), n.extra.get('modelInstrument')): n.key
               for n in baseline.notes if not _inside(n, left, right) and n.extra.get('role') != 'accompaniment'}
    generated, _ = build_score(merged, baseline.name, instrument=params.get('instrument', 'Piano'),
        mode=info.get('mode', params.get('mode', 'auto')), minimum=params.get('minimum', 80),
        shift=info.get('shift', params.get('shift', 0)), bpm=baseline.bpm,
        quantize=info.get('quantize', 0), rhythm=info.get('rhythm', 'gentle'),
        grid_offset_ms=info.get('grid_offset_ms', 0), overrides=params.get('overrides', ()),
        use_detected_beats=info.get('use_detected_beats', True), author=baseline.author, key_anchors=anchors)
    outer = [copy.deepcopy(n) for n in baseline.notes if not _inside(n, left, right)]
    inner, protected_collisions = [], []
    instrument = params.get('instrument', 'Piano')
    rich = info.get('mode') == 'rich_chords'
    if rich:
        from .rich_harmony import MAXIMUM_KEYS, below_lead, sounding_end, strong_tails
        tails = strong_tails(instrument)
    else:
        tails = piano_tails() if instrument == 'Piano' else (0,) * 15
    for n in generated.notes:
        if not _inside(n, left, right):
            if n.extra.get('modelEvent') in added_indices and _overlap(n.time_ms, n.time_ms+n.duration_ms, left, right):
                conflicts.append(f'{n.time_ms/1000:.2f}s 映射后音符跨过选区边界，请调整范围')
            if (rich and n.extra.get('generated') and _overlap(n.time_ms, n.time_ms+n.duration_ms, left, right)
                    and not any((p.key, p.time_ms, p.duration_ms) == (n.key, n.time_ms, n.duration_ms)
                                for p in outer)):
                conflicts.append(f'{n.time_ms/1000:.2f}s 新补全伴奏跨过受保护边界，请调整范围')
            continue
        support = n.extra.get('role') == 'accompaniment'
        blocked = False
        for p in outer:
            p_support = p.extra.get('role') == 'accompaniment'
            end = n.time_ms + (max(n.duration_ms, tails[n.key]) if support else n.duration_ms)
            p_end = p.time_ms + (max(p.duration_ms, tails[p.key]) if p_support else p.duration_ms)
            if rich:
                end = max(end, sounding_end(n)) if support else end
                p_end = max(p_end, sounding_end(p)) if p_support else p_end
            if _overlap(n.time_ms, end, p.time_ms, p_end) <= 1e-6:
                continue
            if (support and ((p_support and not rich) or n.key == p.key)) or (not support and p_support and n.key == p.key):
                blocked = True
                if not support:
                    conflicts.append(f'{n.time_ms/1000:.2f}s 旋律与选区外受保护伴奏同键冲突')
                    protected_collisions.append(dict(protected_interval=[p.time_ms, p_end],
                                                       candidate_interval=[n.time_ms, end], role='accompaniment'))
                break
            if rich and support and p_support:
                from ..midi import SKY_MIDI
                if abs(SKY_MIDI[n.key]-SKY_MIDI[p.key]) % 12 in (1, 6, 11):
                    blocked = True
                    break
            if rich and support != p_support and not below_lead(n.key if support else p.key, [p if support else n]):
                blocked = True
                if not support:
                    conflicts.append(f'{n.time_ms/1000:.2f}s 受保护伴奏侵入新旋律音区，请扩大精修范围')
                    protected_collisions.append(dict(protected_interval=[p.time_ms, p_end],
                                                     candidate_interval=[n.time_ms, end], role='accompaniment'))
                break
            if not support and not p_support:
                # A protected cross-boundary lead may never be shortened to fit a new attack.
                conflicts.append(f'{n.time_ms/1000:.2f}s 新起音与跨界持续旋律冲突，请扩大范围')
                protected_collisions.append(dict(protected_interval=[p.time_ms, p_end],
                                                   candidate_interval=[n.time_ms, end], role='melody'))
                blocked = True
                break
        if rich and not blocked:
            end = sounding_end(n) if support else n.time_ms+n.duration_ms
            sounding = [p for p in outer+inner if _overlap(p.time_ms, sounding_end(p), n.time_ms, end) > 1e-3]
            for t in {n.time_ms, *(p.time_ms for p in sounding if n.time_ms <= p.time_ms < end-1e-3)}:
                active = [p for p in sounding if p.time_ms <= t+1e-3 and sounding_end(p) > t+1e-3]
                support_count = sum(p.extra.get('role') == 'accompaniment' for p in active)
                budget = n.extra.get('maximumSupport', 2) if support else 2
                if len(active) >= MAXIMUM_KEYS or support_count+int(support) > budget:
                    blocked = True
                    if not support:
                        conflicts.append('局部旋律与受保护声部超过三键，请扩大精修范围')
                    break
        if not blocked:
            inner.append(n)
    if rich:
        # A rebuilt anchor may be outside the patch or rejected at its boundary.
        # Align against the actual retained lead, never against a discarded note.
        lead_onsets = {n.time_ms for n in outer+inner if n.extra.get('role') != 'accompaniment'}
        inner = [n for n in inner if n.extra.get('attackAlignment') != 'melody' or n.time_ms in lead_onsets]
    score = copy.deepcopy(baseline)
    score.notes = sorted(outer+inner, key=lambda n: (n.time_ms, n.key))
    score.path = None
    score.root['transcription'] = dict(info, refinement=merged.metadata['refinement'],
        sections=_sections(info.get('sections', []), generated.root['transcription'].get('sections', []), left, right))
    if rich:
        for field in ('chords', 'harmony_review'):
            score.root['transcription'][field] = (
                [s for s in info.get(field, []) if s['end_ms'] <= left or s['start_ms'] >= right] +
                [s for s in generated.root['transcription'].get(field, [])
                 if left <= s['start_ms'] and s['end_ms'] <= right])
    # Mapping and budget evidence applies to the patch only, never a full-song claim.
    score.root['transcription']['refinement']['patch_clarity'] = generated.root['transcription'].get('clarity', {})
    if rich:
        score.root['transcription']['refinement']['patch_structure'] = {
            field: generated.root['transcription'].get(field, [] if field == 'harmony_phrases' else {})
            for field in ('harmony_rhythm', 'harmony_phrases', 'harmony_alignment')}
        for n in inner:
            if n.extra.get('role') == 'accompaniment':
                n.extra['structureScope'] = f'refinement:{ident}'
    before = [asdict(n) for n in baseline.notes if _inside(n, left, right)]
    after = [asdict(n) for n in inner]
    diagnostics = dict(before_notes=len(before), after_notes=len(after), protected_notes=len(outer),
        unchanged=before == after, original_model=original.metadata.get('model'),
        candidate_model=local.metadata.get('model'), cache_hit=local.metadata.get('cache_hit', False),
        load_seconds=local.metadata.get('load_seconds'), inference_seconds=local.metadata.get('inference_seconds'),
        boundary_policy='protect-complete-notes-no-key-only-merge')
    diagnostics['difference'] = note_diff(baseline, score)
    diagnostics['protected_collisions'] = protected_collisions
    diagnostics['unchanged'] = not (diagnostics['difference']['removed'] or diagnostics['difference']['added'])
    return Candidate(ident, parent_id, fingerprint(original, baseline, params), region,
                     merged, score, sorted(set(conflicts)), diagnostics)


class CandidateStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.root = self.directory/'transcription/refinements'
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, ident):
        if len(ident) != 32 or any(c not in '0123456789abcdef' for c in ident):
            raise ValueError('精修 ID 无效')
        return self.root/ident

    def save(self, candidate, local):
        target = self.path(candidate.ident)
        stage = self.root/('.'+candidate.ident)
        stage.mkdir()
        try:
            source = DraftStore(self.directory).path(candidate.parent_id)/'source.audio'
            if source.exists():
                candidate.diagnostics['source_sha256'] = source_digest(source)
            candidate.result.save(stage/'events.json')
            local.save(stage/'local-events.json')
            atomic_json(stage/'candidate.json', dict(schema=1, id=candidate.ident, parent_id=candidate.parent_id,
                baseline=candidate.baseline, region=asdict(candidate.region), score=candidate.score.to_data(),
                conflicts=candidate.conflicts, diagnostics=candidate.diagnostics))
            os.replace(stage, target)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def pending(self, parent_id):
        matches = []
        for file in self.root.glob('*/candidate.json'):
            try:
                d = json.loads(file.read_text(encoding="utf-8"))
                if d['parent_id'] == parent_id and not d.get('resolved'):
                    matches.append((file.stat().st_mtime_ns, d))
            except (OSError, ValueError, KeyError):
                continue
        if not matches:
            return None
        d = max(matches, key=lambda x: x[0])[1]
        return Candidate(d['id'], d['parent_id'], d['baseline'], Region(**d['region']),
                         EventResult.load(self.path(d['id'])/'events.json'), parse_score(json.dumps(d['score'])),
                         d['conflicts'], d['diagnostics'])

    def discard(self, candidate):
        shutil.rmtree(self.path(candidate.ident))

    def accept(self, candidate):
        if candidate.conflicts:
            raise ValueError('存在边界冲突，不能接受；请扩大选区重新生成')
        drafts = DraftStore(self.directory)
        manifest = self.path(candidate.ident)/'candidate.json'
        d = json.loads(manifest.read_text(encoding="utf-8"))
        # A retry after publication (including a crash before the resolved write)
        # returns the same child, never another apparently accepted version.
        ident = uuid.uuid5(uuid.NAMESPACE_URL, 'autoskyplay:refinement:'+candidate.ident).hex
        if drafts.path(ident).exists():
            published = json.loads((drafts.path(ident)/'draft.json').read_text(encoding="utf-8"))
            if published.get('metadata', {}).get('refinement', {}).get('id') != candidate.ident:
                raise ValueError('新版本 ID 冲突，请重新生成候选')
            d.update(resolved='accepted', child_id=ident)
            atomic_json(manifest, d)
            return ident
        data, song, original, score = drafts.load(candidate.parent_id)
        if fingerprint(original, score, data['params']) != candidate.baseline:
            raise ValueError('原草稿已改变，候选已过期，请重新生成')
        source = drafts.path(candidate.parent_id)/'source.audio'
        if not source.exists():
            raise ValueError('草稿源音频已丢失，不能接受为完整新版本')
        expected = candidate.diagnostics.get('source_sha256') or original.metadata.get('source_sha256')
        if expected and source_digest(source) != expected:
            raise ValueError('草稿源音频已改变，候选已过期')
        # Build a complete new draft in a separate store, then publish its directory.
        staging_root = self.path(candidate.ident)/'accepting'
        staging = DraftStore(staging_root)
        child = copy.deepcopy(candidate.score)
        child.name = data.get('title', song.name)+' · 精修'
        try:
            staging.save(ident, song, candidate.result, child, data['params'], edited=True,
                                 source=drafts.path(candidate.parent_id), media=drafts.path(candidate.parent_id),
                                 title=child.name)
            shutil.copy2(self.path(candidate.ident)/'local-events.json', staging.path(ident)/'refinement-local-events.json')
            os.replace(staging.path(ident), drafts.path(ident))
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
        d.update(resolved='accepted', child_id=ident)
        atomic_json(manifest, d)
        return ident
