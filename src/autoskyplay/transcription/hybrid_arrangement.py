"""Phrase routing using input provenance; never merge adjacent equal pitches."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import replace

import numpy as np

from .events import _line
from .segmentation import runs


def arrange_hybrid(result, minimum=80, mode="simple", overrides=()):
    hop = result.analysis.get("evidence_hop_ms", 100)
    size = math.ceil(result.duration_ms / hop)
    energy = np.asarray(result.analysis.get("energy_db", [-100] * size))
    vocal_energy = np.asarray(result.analysis.get("vocals_energy_db", [-100] * size))
    ratio = np.asarray(result.analysis.get("vocals_ratio", [0] * size))
    if any(len(a) != size for a in (energy, vocal_energy, ratio)):
        raise ValueError("混合音轨证据长度无效")
    decisions, vocals = [], []

    def record(n, action, reason):
        decisions.append(dict(event_id=n.identity, action=action, reason=reason))

    def span(n):
        return slice(int(n.start_ms / hop), min(size, math.ceil((n.start_ms + n.duration_ms) / hop)))

    # Presence is an acoustic guard, not a calibrated classifier or model score.
    guard = (vocal_energy >= -50) & (ratio >= 0.22)
    for n in result.raw_notes:
        if n.audio_source != "vocals":
            continue
        if n.duration_ms < minimum or not 45 <= n.pitch <= 96:
            record(n, "omit", "人声候选过短或超出当前旋律识别范围")
            continue
        supported = (vocal_energy[span(n)] >= -55) & (ratio[span(n)] >= 0.08)
        if not len(supported) or np.mean(supported) < 0.6:
            record(n, "review", "人声候选缺少持续的音轨支持")
            continue
        vocals.append(n)
        guard[span(n)] = True
    # Bridge a breath only in the source guard, never fill it with a note.
    for a, b, active in runs(guard.copy()):
        if not active and a > 0 and b < size and (b - a) * hop <= 1200:
            guard[a:b] = True
    groups = defaultdict(list)
    for n in result.raw_notes:
        if n.audio_source == "vocals":
            continue
        if n.instrument == "voice":
            record(n, "omit", "背景分支的人声标签疑似残留，不能作为主唱")
            continue
        if n.instrument in ("drums", "electric_bass", "acoustic_bass") or n.duration_ms < minimum:
            continue
        if not 45 <= n.pitch <= 96 or np.max(energy[span(n)], initial=-100) < -60:
            continue
        if guard[span(n)].any():
            record(n, "omit", "保护人声乐句及换气；不截断器乐候选后塞入空隙")
            continue
        groups[(n.provider, n.audio_source, n.instrument)].append(n)
    lines = {name: _line(notes) for name, notes in groups.items()}
    instrumental, routes = [], []
    previous = None
    for a, b, active in runs(guard):
        if active or (b - a) * hop < 1200:
            continue
        # Decisions may use windows, but accepted events retain their entire ends.
        for begin in range(a * hop, b * hop, 4000):
            end = min(b * hop, begin + 4000)
            candidates = []
            for name, (line, monophony) in lines.items():
                notes = [
                    n for n in line if n.start_ms < end + 1000 and n.start_ms + n.duration_ms > begin - 1000
                ]
                if not notes or len(notes) == 1 and notes[0].duration_ms < 600:
                    continue
                coverage = min(
                    1,
                    sum(max(0, min(end, n.start_ms + n.duration_ms) - max(begin, n.start_ms)) for n in notes)
                    / max(1, end - begin),
                )
                jumps = (
                    np.mean([min(24, abs(x.pitch - y.pitch)) for x, y in zip(notes, notes[1:])])
                    if len(notes) > 1
                    else 0
                )
                score = coverage + 0.7 * monophony - 0.025 * jumps + (0.2 if name == previous else 0)
                candidates.append((float(score), name))
            candidates.sort(reverse=True)
            winner = candidates[0][1] if candidates and candidates[0][0] >= 0.7 else None
            if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.18:
                winner = None
            routes.append(
                dict(
                    start_ms=begin,
                    end_ms=min(result.duration_ms, end),
                    winner=list(winner) if winner else None,
                    scores=[dict(source=list(k), arrangement_score=v) for v, k in candidates],
                )
            )
            if winner:
                previous = winner
                instrumental.extend(n for n in lines[winner][0] if begin <= n.start_ms < end)
    chosen = sorted([*vocals, *instrumental], key=lambda n: (n.start_ms, n.index))
    selected = []
    for n in chosen:
        if selected and n.start_ms < selected[-1].start_ms + selected[-1].duration_ms - 1e-5:
            last = selected[-1]
            if (last.provider, last.audio_source, last.instrument) != (
                n.provider,
                n.audio_source,
                n.instrument,
            ):
                record(n, "omit", "避免来源切换截断已建立的长音")
                continue
            if n.start_ms <= last.start_ms:
                record(n, "omit", "同起音复音候选竞争")
                continue
            selected[-1] = replace(last, duration_ms=n.start_ms - last.start_ms)
            record(last, "trim_overlap", "同声部后续起音形成单音旋律边界")
        selected.append(n)
        record(n, "melody", "可靠人声乐句" if n.audio_source == "vocals" else "连续器乐候选")
    notes = [n.melody() for n in selected]
    for n in selected:
        for reason in n.review_reasons:
            record(n, "review_preserved_boundary", reason)
    for override in overrides:
        a, b, source = override["start_ms"], override["end_ms"], override["source"]
        if (
            source not in ("vocals", "other", "rest")
            or not np.isfinite([a, b]).all()
            or not 0 <= a < b <= result.duration_ms + 1
        ):
            raise ValueError("段落来源覆盖无效")
        kept = []
        for n in notes:
            end = n.start_ms + n.duration_ms
            if end <= a or n.start_ms >= b or n.source == source:
                kept.append(n)
            else:
                if n.start_ms < a:
                    kept.append(replace(n, duration_ms=a - n.start_ms))
                if end > b:
                    kept.append(replace(n, start_ms=b, duration_ms=end - b))
        notes = kept
    boundaries = sorted(
        {
            0.0,
            result.duration_ms,
            *(n.start_ms for n in notes),
            *(n.start_ms + n.duration_ms for n in notes),
            *(v for o in overrides for v in (o["start_ms"], o["end_ms"])),
        }
    )
    sections = []
    for a, b in zip(boundaries, boundaries[1:]):
        active = next((n for n in notes if n.start_ms <= a and n.start_ms + n.duration_ms >= b - 1e-5), None)
        source = active.source if active else "rest"
        sl = slice(int(a / hop), max(int(a / hop) + 1, math.ceil(b / hop)))
        audible = np.max(energy[sl], initial=-100) > -60
        unclear_voice = bool(np.any((vocal_energy[sl] >= -50) & (ratio[sl] >= 0.22)))
        if active is None and audible and (unclear_voice or b - a >= 1200):
            source = "uncertain"
        if any(o["source"] == "rest" and o["start_ms"] <= a and o["end_ms"] >= b for o in overrides):
            source = "rest"
        section = dict(
            start_ms=a,
            end_ms=b,
            source=source,
            needs_review=source == "uncertain",
            reason="存在音频但缺少可靠旋律候选，保留休止" if source == "uncertain" else "",
        )
        if sections and sections[-1]["source"] == source:
            sections[-1]["end_ms"] = b
        else:
            sections.append(section)
    if mode == "simple":
        chosen_ids = {n.event_id for n in notes}
        for n in result.raw_notes:
            if n.audio_source == "vocals" or n.instrument in ("voice", "drums") or n.identity in chosen_ids:
                continue
            if n.duration_ms < max(80, minimum):
                continue
            duplicate = None
            for v in vocals:
                overlap = max(
                    0,
                    min(v.start_ms + v.duration_ms, n.start_ms + n.duration_ms) - max(v.start_ms, n.start_ms),
                )
                distance = abs((v.raw_pitch_midi if v.raw_pitch_midi is not None else v.pitch) - n.pitch)
                if (
                    overlap >= 0.5 * min(v.duration_ms, n.duration_ms)
                    and min(distance, abs(distance - 12)) < 0.6
                ):
                    duplicate = v
                    break
            if duplicate:
                record(n, "omit", "省略与主唱同音／八度重叠的背景支撑；原始事件保留")
                continue
            active = next(
                (
                    m
                    for m in notes
                    if m.role == "melody"
                    and m.start_ms <= n.start_ms < m.start_ms + m.duration_ms
                    and m.pitch - n.pitch >= 5
                ),
                None,
            )
            if active:
                duration = min(n.start_ms + n.duration_ms, active.start_ms + active.duration_ms) - n.start_ms
                if duration >= max(80, minimum):
                    notes.append(replace(n.melody("accompaniment"), duration_ms=duration))
                    record(n, "accompaniment_candidate", "实际背景音符，经光遇稀疏规则继续筛选")
    return sorted(notes, key=lambda n: (n.start_ms, n.role)), dict(
        sections=sections,
        chords=[],
        routing=routes,
        event_decisions=decisions,
        boundary_reviews=result.analysis.get("boundary_reviews", []),
        game_window_review=result.analysis.get("game_window_review", []),
        warnings=[
            "混合后端为实验选项；GAME 不提供逐音概率，分轨能量和编配评分仅作为来源证据。",
            "未自动合并相邻同音。原始小数音高保留于事件数据，光遇与原始 MIDI 使用整数音高。",
        ],
    )
