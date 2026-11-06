"""Experimental direct realization of MOSS chord symbols on the 15 Sky keys."""
from __future__ import annotations

from bisect import bisect_left
from itertools import accumulate

from ..midi import SKY_MIDI, WHITE, to_sky_key
from ..score import Note


def direct_accompaniment(score, result):
    """Play each recognized chord once; do not filter it using musical evidence.

    The model supplies symbols and change times, not voicing or rhythm. A chord
    is therefore sounded at its reported start until the next change, with the
    final chord lasting to the end of the analyzed clip. Sky's white-key mapping
    and the impossibility of triggering the same key twice at once still apply.
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
        meta['warnings'].append('MOSS 直接编配没有可用和弦；请先对当前草稿运行 MOSS 分析。')
    elif legacy:
        meta['warnings'].append('旧版 MOSS 结果只有三和弦且末尾限时；重新分析可使用完整和弦输出。')

    occupied = {(n.time_ms, n.key) for n in score.notes}
    lead_intervals = {}
    for key in range(15):
        spans = sorted((n.time_ms, n.time_ms+n.duration_ms) for n in score.notes
                       if n.key == key and n.extra.get('role') == 'melody')
        lead_intervals[key] = ([a for a, _ in spans], list(accumulate((b for _, b in spans), max)))
    added, unsupported, collisions, approximated = [], [], 0, 0
    shift = meta['shift']
    for chord in raw:
        if chord['intervals'] is None:
            unsupported.append(dict(start_ms=chord['start_ms'], symbol=chord['symbol']))
            continue
        pitches = ([48 + chord['bass']] if chord['bass'] is not None else []) + [
            60 + chord['root'] + interval for interval in chord['intervals']]
        for pitch in pitches:
            key = to_sky_key(pitch + shift)
            identity = (chord['start_ms'], key)
            if identity in occupied:
                collisions += 1
                continue
            occupied.add(identity)
            is_approximate = (pitch + shift) % 12 not in WHITE
            approximated += is_approximate
            added.append(Note(key, chord['start_ms'], chord['end_ms'] - chord['start_ms'], dict(
                sourcePitch=pitch, sourceTime=chord['start_ms'], sourceDuration=chord['end_ms']-chord['start_ms'],
                pitchConfidence=None, role='accompaniment', source='moss', generated=True,
                harmonySymbol=chord['symbol'], mossInferredEnd=chord['inferred_end'],
                approximated=is_approximate, folded=abs(SKY_MIDI[key] - (pitch + shift)) >= 11)))
    score.notes = sorted(score.notes + added, key=lambda n: (n.time_ms, n.key))
    lead_overlaps = 0
    for note in added:
        starts, ends = lead_intervals[note.key]
        i = bisect_left(starts, note.time_ms + note.duration_ms) - 1
        lead_overlaps += i >= 0 and ends[i] > note.time_ms
    if unsupported:
        meta['warnings'].append(f"有 {len(unsupported)} 个 MOSS 和弦符号暂不支持，未转换；详见 MOSS 和弦信息。")
    if lead_overlaps:
        meta['warnings'].append(f'{lead_overlaps} 个伴奏音与旋律同键重叠，演奏时可能重触或影响长音。')
    meta['moss_direct'] = dict(policy='moss-direct-v1', chords=raw, unsupported=unsupported,
                               notes=len(added), approximated=approximated, simultaneous_key_collisions=collisions,
                               melody_key_overlaps=lead_overlaps,
                               last_end_inferred=any(c['inferred_end'] for c in raw))
    meta['warnings'].append('MOSS 直接编配未经过原有和声、音区、音尾或同时按键限制；半音按光遇白键近似。')
    return score
