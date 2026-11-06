"""Melody-aware voicings for MOSS chord symbols on Sky's white keys."""
from __future__ import annotations

from bisect import bisect_left

from ..midi import SKY_MIDI, WHITE, to_sky_key
from ..score import Note


def available_parts(start, end, blockers, minimum):
    """Subtract melody key holds and explicit rests from a chord interval."""
    parts = [(start, end)]
    for a, b in blockers:
        if a >= end:
            break
        parts = [(x, y) for left, right in parts
                 for x, y in ([(left, right)] if b <= left or a >= right else
                              [(left, min(right, a)), (max(left, b), right)]) if y - x >= minimum]
    return parts


def optimized_accompaniment(score, result):
    """Prefer exact chord tones and octave choices that leave melody keys free.

    The original melody and MOSS chord-change times stay untouched. Black-key
    chord tones are omitted when at least one exact tone is available; exact
    chord tones can be doubled in another octave. For chords made entirely of
    black-key pitches, nearest-white-key approximations remain as a fallback.
    """
    meta = score.root['transcription']
    analysis = getattr(result, 'analysis', {}).get('moss_music') or {}
    raw = analysis.get('raw_chords')
    legacy = raw is None
    if legacy:
        raw = [dict(c, symbol=f"{c['root']}:{c['quality']}",
                    intervals=[0, 3 if c['quality'] == 'minor' else 4, 7],
                    bass=None, inferred_end=False) for c in analysis.get('chords', [])]
    if not raw:
        meta['warnings'].append('MOSS 优化编配没有可用和弦；请先对当前草稿运行 MOSS 分析。')
    elif legacy:
        meta['warnings'].append('旧版 MOSS 结果只有三和弦且末尾限时；重新分析可使用完整和弦输出。')

    from .rich_harmony import strong_tails

    lead = [n for n in score.notes if n.extra.get('role') == 'melody']
    tails = strong_tails(meta['instrument'])
    rests = [(o['start_ms'], o['end_ms']) for o in meta.get('overrides', []) if o['source'] == 'rest']
    blocked = {key: sorted(rests + [(n.time_ms, n.time_ms+n.duration_ms)
                                    for n in lead if n.key == key])
               for key in range(15)}
    lead_starts = {key: sorted(n.time_ms for n in lead if n.key == key) for key in range(15)}
    shift = meta['shift']
    added, unsupported, decisions = [], [], []
    omitted_chromatic = doubled = approximate = omitted_blocked = omitted_voice_limit = 0
    previous_pitches = []
    for chord in raw:
        if chord['intervals'] is None:
            unsupported.append(dict(start_ms=chord['start_ms'], symbol=chord['symbol']))
            continue
        pitches = ([48 + chord['bass']] if chord['bass'] is not None else []) + [
            60 + chord['root'] + interval for interval in chord['intervals']]
        # Preserve distinct pitch classes; a slash bass can share a chord tone.
        distinct, seen = [], set()
        for pitch in pitches:
            if pitch % 12 not in seen:
                distinct.append(pitch)
                seen.add(pitch % 12)
        exact = [p for p in distinct if (p + shift) % 12 in WHITE]
        source = exact if exact else distinct
        omitted = [p for p in distinct if p not in exact] if exact else []
        omitted_chromatic += len(omitted)
        target = min(len(distinct), 4)
        candidates = [(p, False) for p in source]
        omitted_for_limit = [p for p, _ in candidates[target:]]
        omitted_voice_limit += len(omitted_for_limit)
        # A second octave of a supported tone adds body without inventing a
        # non-chord pitch; limit doubling to one extra voice per pitch class.
        if exact and len(candidates) < target:
            candidates += [(p, True) for p in exact[:target-len(candidates)]]
        used = set()
        current_pitches = []
        note_start_index = len(added)
        for pitch, is_double in candidates[:target]:
            raw_pitch = pitch + shift
            approximated = raw_pitch % 12 not in WHITE
            wanted_pc = SKY_MIDI[to_sky_key(raw_pitch)] % 12 if approximated else raw_pitch % 12
            choices = []
            for key, playable in enumerate(SKY_MIDI):
                if key in used or playable % 12 != wanted_pc:
                    continue
                parts = available_parts(chord['start_ms'], chord['end_ms'], blocked[key], 80)
                # A piano sample rings independently of the written duration.
                # Delay or omit an attack whose strong tail would overlap a
                # same-key melody attack, without shortening safe notes merely
                # because a later melody note exists.
                safe = []
                starts = lead_starts[key]
                for a, b in parts:
                    previous = bisect_left(starts, a) - 1
                    if previous >= 0:
                        a = max(a, starts[previous] + tails[key])
                    following = bisect_left(starts, a)
                    if following < len(starts) and starts[following] < a + tails[key]:
                        continue
                    if b - a >= 80:
                        safe.append((a, b))
                parts = safe
                coverage = sum(b-a for a, b in parts)
                if not parts:
                    continue
                # Prefer uninterrupted coverage, then a compact lower voicing
                # and small movements from the previous chord.
                preferred = 72 if is_double else 60 + (raw_pitch % 12)
                continuity = min((abs(playable-p) for p in previous_pitches), default=0)
                choices.append((coverage, -len(parts), -abs(playable-preferred), -continuity, -key, key, parts))
            if not choices:
                omitted_blocked += 1
                continue
            *_, key, parts = max(choices)
            used.add(key)
            current_pitches.append(SKY_MIDI[key])
            doubled += is_double
            approximate += approximated
            for start, end in parts:
                added.append(Note(key, start, end-start, dict(
                    sourcePitch=pitch, sourceTime=chord['start_ms'], sourceDuration=chord['end_ms']-chord['start_ms'],
                    pitchConfidence=None, role='accompaniment', source='moss', generated=True,
                    harmonySymbol=chord['symbol'], mossInferredEnd=chord['inferred_end'],
                    mossDoubled=is_double, approximated=approximated,
                    folded=abs(SKY_MIDI[key]-raw_pitch) >= 11)))
        if current_pitches:
            previous_pitches = current_pitches
        decisions.append(dict(symbol=chord['symbol'], start_ms=chord['start_ms'], end_ms=chord['end_ms'],
                              omitted_pitch_classes=sorted({p % 12 for p in omitted}),
                              omitted_for_voice_limit=sorted({p % 12 for p in omitted_for_limit}),
                              fallback_all_chromatic=not bool(exact), voices=len(current_pitches),
                              note_fragments=len(added)-note_start_index))
    score.notes = sorted(score.notes + added, key=lambda n: (n.time_ms, n.key))
    if unsupported:
        meta['warnings'].append(f'{len(unsupported)} 个 MOSS 和弦符号暂不支持，未转换。')
    if omitted_chromatic:
        meta['warnings'].append(f'已省略 {omitted_chromatic} 个无法精确演奏的半音和弦音；部分和弦改为同音八度加厚。')
    if approximate:
        meta['warnings'].append(f'{approximate} 个声部属于无法用白键精确演奏的和弦，仍采用半音近似。')
    if omitted_voice_limit:
        meta['warnings'].append(f'四声部上限省略了 {omitted_voice_limit} 个和弦音。')
    silent_chords = sum(d['voices'] == 0 for d in decisions)
    if silent_chords:
        meta['warnings'].append(f'{silent_chords} 个和弦因旋律同键或人工休止没有可演奏声部。')
    meta['moss_optimized'] = dict(policy='moss-voicing-v1', chords=raw, unsupported=unsupported,
        notes=len(added), omitted_chromatic=omitted_chromatic, octave_doubled=doubled,
        approximated_voices=approximate, omitted_blocked=omitted_blocked, omitted_voice_limit=omitted_voice_limit,
        strong_tail_guard_db=-18, decisions=decisions, silent_chords=silent_chords,
        last_end_inferred=any(c['inferred_end'] for c in raw))
    return score
