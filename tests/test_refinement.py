import copy
from dataclasses import asdict

import numpy as np
import pytest

from autoskyplay.netease import NeteaseSong
from autoskyplay.score import Note, Score
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.audition import render_window
from autoskyplay.transcription.drafts import DraftStore
from autoskyplay.transcription.events import EventResult, ModelNote
from autoskyplay.transcription.performance import generation_mode, sparse_chords
from autoskyplay.transcription.refinement import (
    CandidateStore,
    Region,
    fingerprint,
    frozen_rhythm,
    make_candidate,
    reference_groups,
)


def fixture():
    result = EventResult([ModelNote(72, t, 500, 'voice', i) for i, t in enumerate(range(0, 10000, 1000))],
        10000, 120, {'model': 'small', 'source_start': 60, 'source_origin': 30},
        {'beats_ms': list(range(0, 10000, 500)), 'beat_confidence': .9, 'energy_db': [-20]*100})
    score, _ = build_score(result, 'Test', mode='melody', shift=0)
    params = dict(instrument='Piano', mode='melody', minimum=80, shift=0, overrides=[])
    return result, score, params


def local_for(result, region, change=False):
    from dataclasses import replace
    return EventResult([replace(n, start_ms=n.start_ms-region.context_start,
                                pitch=74 if change and n.start_ms == 4000 else n.pitch)
                        for n in result.raw_notes if region.context_start <= n.start_ms and
                        n.start_ms+n.duration_ms <= region.context_end],
                       region.context_end-region.context_start, result.bpm, {'model': 'small'})


def test_reference_pulses_are_selection_only_and_low_confidence_has_no_bars():
    result, score, params = fixture()
    before = fingerprint(result, score, params)
    assert reference_groups(result, 3, 1000)[:3] == [1000, 2500, 4000]
    assert fingerprint(result, score, params) == before
    result.analysis['beat_confidence'] = .1
    assert reference_groups(result) == []


def test_limits_and_frozen_local_time_offset():
    r, _, _ = fixture()
    region = Region.create(r, 4000, 6000)
    assert region.context_start == 2000 and region.context_end == 8000
    assert frozen_rhythm(r, region)['beats_ms'][0] == 0
    assert r.metadata['source_start'] + region.context_start/1000 - r.metadata['source_origin'] == 32
    for a, b in ((-1, 1000), (0, 11000), (1000, 1000), (0, float('nan'))):
        with pytest.raises(ValueError):
            Region.create(r, a, b)


def test_outside_manual_edits_and_all_intervals_unchanged():
    r, score, params = fixture()
    score.notes[0].key = 2
    score.notes[-1].duration_ms = 850
    region = Region.create(r, 3000, 6000)
    before = copy.deepcopy(score.to_data())
    c = make_candidate('a'*32, r, score, params, local_for(r, region, True), region)
    assert not c.conflicts
    assert score.to_data() == before
    assert [asdict(n) for n in c.score.notes if n.time_ms < 3000 or n.time_ms >= 6000] == [
        asdict(n) for n in score.notes if n.time_ms < 3000 or n.time_ms >= 6000]
    assert next(n for n in c.score.notes if n.time_ms == 4000).key != next(n for n in score.notes if n.time_ms == 4000).key
    assert c.result.analysis == r.analysis and c.score.bpm == score.bpm


def test_crossing_candidate_cannot_be_accepted_and_original_hold_is_preserved(tmp_path):
    r, score, params = fixture()
    score.notes[2].duration_ms = 1800
    region = Region.create(r, 3000, 6000)
    local = local_for(r, region)
    local.raw_notes.append(ModelNote(76, 1800, 1500, 'voice', 100))
    c = make_candidate('a'*32, r, score, params, local, region)
    assert c.conflicts
    assert next(n for n in c.score.notes if n.time_ms == 2000).duration_ms == 1800
    with pytest.raises(ValueError, match='边界'):
        CandidateStore(tmp_path).accept(c)


def test_repeated_attacks_and_manual_rest_survive():
    r, score, params = fixture()
    region = Region.create(r, 3000, 6000)
    local = EventResult([ModelNote(72, t, 300, 'voice', i) for i, t in enumerate((2000, 2450, 3000, 4000))],
                        7000, metadata={'model': 'small'})
    params['overrides'] = [dict(start_ms=3900, end_ms=4400, source='rest')]
    c = make_candidate('a'*32, r, score, params, local, region)
    notes = [n for n in c.score.notes if 3000 <= n.time_ms < 6000]
    assert any(n.time_ms == 3000 for n in notes) and any(n.time_ms == 3450 for n in notes)
    assert not any(n.time_ms < 4400 and n.time_ms+n.duration_ms > 3900 for n in notes)


def test_pending_accept_forks_and_stale_rejected(tmp_path):
    r, score, params = fixture()
    drafts = DraftStore(tmp_path)
    source = tmp_path/'source'
    source.mkdir()
    (source/'source.audio').write_bytes(b'original source')
    song = NeteaseSong('1', 'Test', '', '', '')
    parent = drafts.save(None, song, r, score, params, edited=True, source=source)
    baseline_bytes = (drafts.path(parent)/'draft.json').read_bytes()
    region = Region.create(r, 3000, 6000)
    local = local_for(r, region, True)
    c = make_candidate(parent, r, score, params, local, region)
    store = CandidateStore(tmp_path)
    store.save(c, local)
    pending = store.pending(parent)
    assert pending.ident == c.ident
    child = store.accept(pending)
    assert child != parent and (drafts.path(parent)/'draft.json').read_bytes() == baseline_bytes
    assert (drafts.path(child)/'source.audio').read_bytes() == b'original source'
    assert (drafts.path(child)/'refinement-local-events.json').exists()
    assert store.pending(parent) is None
    assert store.accept(c) == child and len(drafts.entries()) == 2
    stale = make_candidate(parent, r, score, params, local, region)
    store.save(stale, local)
    changed = copy.deepcopy(score)
    changed.notes[0].key += 1
    drafts.save(parent, song, r, changed, params, edited=True)
    with pytest.raises(ValueError, match='过期'):
        store.accept(stale)


def test_failed_accept_never_exposes_partial_draft(tmp_path, monkeypatch):
    r, score, params = fixture()
    drafts = DraftStore(tmp_path)
    parent = drafts.save(None, NeteaseSong('1', 'A', '', '', ''), r, score, params)
    (drafts.path(parent)/'source.audio').write_bytes(b'source')
    region = Region.create(r, 3000, 6000)
    local = local_for(r, region)
    c = make_candidate(parent, r, score, params, local, region)
    store = CandidateStore(tmp_path)
    store.save(c, local)
    import autoskyplay.transcription.refinement as module
    monkeypatch.setattr(module.shutil, 'copy2', lambda *_: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        store.accept(c)
    assert len(drafts.entries()) == 1 and store.pending(parent)


def test_accept_recovers_after_publication_before_manifest_write(tmp_path, monkeypatch):
    import autoskyplay.transcription.refinement as module
    r, score, params = fixture()
    drafts = DraftStore(tmp_path)
    parent = drafts.save(None, NeteaseSong('1', 'A', '', '', ''), r, score, params)
    (drafts.path(parent)/'source.audio').write_bytes(b'source')
    region = Region.create(r, 3000, 6000)
    local = local_for(r, region, True)
    c = make_candidate(parent, r, score, params, local, region)
    store = CandidateStore(tmp_path)
    store.save(c, local)
    write = module.atomic_json
    def fail_after_publish(path, data):
        if data.get('resolved'):
            raise OSError('interrupted after publish')
        write(path, data)
    monkeypatch.setattr(module, 'atomic_json', fail_after_publish)
    with pytest.raises(OSError):
        store.accept(c)
    assert len(drafts.entries()) == 2 and store.pending(parent)
    monkeypatch.setattr(module, 'atomic_json', write)
    child = store.accept(c)
    assert child != parent and len(drafts.entries()) == 2 and not store.pending(parent)


def test_changed_audio_and_wrong_context_rejected(tmp_path):
    r, score, params = fixture()
    drafts = DraftStore(tmp_path)
    parent = drafts.save(None, NeteaseSong('1', 'A', '', '', ''), r, score, params)
    path = drafts.path(parent)/'source.audio'
    path.write_bytes(b'source')
    region = Region.create(r, 3000, 6000)
    local = local_for(r, region, True)
    c = make_candidate(parent, r, score, params, local, region)
    store = CandidateStore(tmp_path)
    store.save(c, local)
    path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='音频已改变'):
        store.accept(c)
    local.duration_ms += 150
    with pytest.raises(ValueError, match='范围'):
        make_candidate(parent, r, score, params, local, region)


def test_chord_mode_keeps_two_evidenced_tones_and_lead_immutable():
    lead = Note(9, 0, 1000, dict(role='melody', sourcePitch=76))  # E5
    supports = [Note(0, 0, 1000, dict(role='accompaniment', sourcePitch=60, sourceTime=0)),
                Note(4, 0, 1000, dict(role='accompaniment', sourcePitch=67, sourceTime=0))]
    score = Score(notes=[lead, *supports], root={'transcription': dict(shift=0, sections=[], minimum_ms=80)})
    original = asdict(lead)
    sparse_chords(score, 'Piano')
    support = [n for n in score.notes if n.extra.get('role') == 'accompaniment']
    assert len(support) == 2
    assert asdict(lead) == original
    assert {n.extra['sourcePitch'] % 12 for n in support} == {0, 7}
    assert score.root['transcription']['clarity']['dyads'] == 1
    assert generation_mode('auto', 'Piano') == 'chords'
    assert generation_mode('auto', 'Violin') == 'melody'
    assert generation_mode('chords', 'Violin') == 'melody'


def test_chords_do_not_fill_protected_rest_or_tiny_notes():
    score = Score(notes=[Note(9, 0, 1000, dict(role='melody', sourcePitch=76)),
                         Note(0, 0, 50, dict(role='accompaniment', sourcePitch=60, sourceTime=0)),
                         Note(4, 0, 800, dict(role='accompaniment', sourcePitch=67, sourceTime=0))],
                  root={'transcription': dict(shift=0, sections=[dict(start_ms=500, end_ms=1000, source='rest')])})
    sparse_chords(score, 'Piano')
    assert all(n.extra['role'] == 'melody' for n in score.notes)


@pytest.mark.parametrize('instrument', ['Piano', 'Violin'])
def test_preview_window_equals_continuous_render_crop(instrument):
    score = Score(notes=[Note(4, 0, 2600), Note(7, 2800, 700)])
    whole = render_window(score, 0, 4000, instrument)
    clip = render_window(score, 1700, 3500, instrument)
    np.testing.assert_allclose(clip, whole[74970:154350], atol=1e-6)
    assert np.max(abs(clip[:1000])) > 1e-4


def test_old_worker_cannot_replace_frozen_clock_and_worker_identity_is_cached(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from autoskyplay.netease import NeteaseAudio
    from autoskyplay.transcription.engine import Engine
    from autoskyplay.transcription.task import TranscriptionTask
    calls = []
    reference = dict(bpm=96, bpm_estimated=True, beats_ms=[100, 725], beat_confidence=.8)
    def run(task):
        calls.append(task.engine.config['runtime_identity'])
        EventResult([ModelNote(72, 0, 500, 'voice', 0)], 1000, 120).save(task.work/'events.json')
        return {}
    monkeypatch.setattr(TranscriptionTask, '_run_engine', run)
    def request(identity):
        engine = Engine([], tmp_path, '2.0.0', 'muscriptor', config={'runtime_identity': identity})
        task = TranscriptionTask(tmp_path, SimpleNamespace(), NeteaseSong('1', '', '', '', ''),
                                 NeteaseAudio('1', '', 10000), engine, 0, 1, rhythm_reference=reference)
        (task.work/'source.audio').write_bytes(b'audio')
        (task.work/'request.json').write_text(json.dumps(dict(start=0, length=1, rhythm_reference=reference)))
        try:
            return task._run_muscriptor()
        finally:
            task.close()
    first = request('old')
    assert first.bpm == 96 and first.analysis['beats_ms'] == [100, 725]
    assert first.metadata['rhythm_reference'] == 'frozen-draft'
    assert request('old').metadata['cache_hit']
    assert not request('new').metadata['cache_hit']
    assert calls == ['old', 'new']


def test_safe_boundaries_expand_to_whole_notes_without_shortening(tmp_path):
    from autoskyplay.transcription.refinement import make_safe_candidate, search_context
    r, score, params = fixture()
    region = search_context(r, Region.create(r, 3250, 6000))
    local = local_for(r, region, True)
    # A new pitch crossing the requested in-point cannot be matched to the old event.
    from dataclasses import replace
    local.raw_notes = [replace(n, pitch=76) if n.start_ms == 3000 else n for n in local.raw_notes]
    exact = make_candidate('a'*32, r, score, params, local, region)
    assert exact.conflicts
    c = make_safe_candidate('a'*32, r, score, params, local, region)
    assert not c.conflicts and c.region.start <= 3000 and c.region.end >= 6000
    assert c.region.start >= region.start-4000 and c.region.end-region.end <= 4000
    new = next(n for n in c.result.raw_notes if n.pitch == 76)
    assert new.start_ms == 3000 and new.duration_ms == 500
    assert c.diagnostics['boundary_search']['requested'] == [3250, 6000]
    assert [asdict(n) for n in c.score.notes if n.time_ms < c.region.start] == [
        asdict(n) for n in score.notes if n.time_ms < c.region.start]


def test_unresolvable_hold_and_maximum_scope_stay_blocked():
    from autoskyplay.transcription.refinement import make_safe_candidate, search_context
    r = EventResult([ModelNote(72, 0, 20000, 'voice', 0)], 30000, metadata={'model': 'small'})
    score, _ = build_score(r, 'long', mode='melody', shift=0)
    region = search_context(r, Region.create(r, 10000, 12000))
    local = EventResult([ModelNote(74, 0, 18000, 'voice', 0)], 18000, metadata={'model': 'medium'})
    c = make_safe_candidate('a'*32, r, score, dict(mode='melody'), local, region)
    assert c.conflicts and c.region == region
    assert asdict(c.score.notes[0]) == asdict(score.notes[0])
    assert c.score.notes[0].duration_ms == 20000
    assert len(c.diagnostics['boundary_search']['attempts']) <= 6


def test_crossing_repeat_candidates_cannot_both_match_one_old_hold():
    from autoskyplay.transcription.refinement import safe_regions
    r = EventResult([ModelNote(72, 2500, 1500, 'voice', 0)], 10000, metadata={'model': 'small'})
    score, _ = build_score(r, 'repeat', mode='melody', shift=0)
    region = Region(3000, 5000, 0, 10000)
    local = EventResult([ModelNote(72, 2500, 1500, 'voice', 0),
                         ModelNote(72, 2600, 1400, 'voice', 1)], 10000, metadata={'model':'small'})
    c = make_candidate('a'*32, r, score, dict(mode='melody'), local, region)
    assert any('一一对应' in message for message in c.conflicts)
    assert not any(p.start == 3000 for p in safe_regions(r, score, local, region))
    assert score.notes[0].duration_ms == 1500


def test_safe_search_keeps_rest_repeated_attacks_and_frozen_legacy_auto_mode():
    from autoskyplay.transcription.refinement import make_safe_candidate, search_context
    r, _, params = fixture()
    params.update(mode='auto', overrides=[dict(start_ms=3900, end_ms=4400, source='rest')])
    baseline, _ = build_score(r, 'old auto', mode='simple', overrides=params['overrides'])
    region = search_context(r, Region.create(r, 3000, 6000))
    local = local_for(r, region)
    local.raw_notes = [n for n in local.raw_notes if n.start_ms != 3000]
    local.raw_notes += [ModelNote(72, 3000, 300, 'voice', 50),ModelNote(72, 3450, 300, 'voice', 51)]
    c = make_safe_candidate('a'*32, r, baseline, params, local, region)
    assert not c.conflicts and c.region == region
    assert c.score.root['transcription']['mode'] == 'simple'
    assert any(n.time_ms == 3000 for n in c.score.notes) and any(n.time_ms == 3450 for n in c.score.notes)
    assert not any(n.time_ms < 4400 and n.time_ms+n.duration_ms > 3900 for n in c.score.notes)
    assert c.result.raw_notes  # The raw source evidence is not cut to implement the rest override.


def test_safe_search_never_shrinks_or_exceeds_thirty_seconds_and_can_cancel():
    from autoskyplay.transcription.refinement import make_safe_candidate, safe_regions, search_context
    r = EventResult([ModelNote(72, t, 800, 'voice', i) for i, t in enumerate(range(0, 50000, 1000))],
                    50000, metadata={'model': 'small'})
    score, _ = build_score(r, 'bounded', mode='melody')
    region = search_context(r, Region.create(r, 10000, 40000))
    local = local_for(r, region)
    choices = safe_regions(r, score, local, region)
    assert choices and all(p.start == 10000 and p.end == 40000 for p in choices)
    with pytest.raises(ValueError, match='4 秒'):
        safe_regions(r, score, local, region, expansion=4001)
    before = score.to_data()
    with pytest.raises(ValueError, match='已取消'):
        make_safe_candidate('a'*32, r, score, dict(mode='melody'), local, region, cancelled=lambda: True)
    assert score.to_data() == before


def test_piano_default_matches_explicit_chords_and_keeps_same_lead():
    notes = [ModelNote(76, t, 900, 'voice', i) for i, t in enumerate(range(0, 8000, 1000))]
    notes += [ModelNote(pitch, t, 800, 'piano', len(notes)+i)
              for i, (t, pitch) in enumerate((t, p) for t in (0, 4000) for p in (48, 55))]
    r = EventResult(notes, 8000, metadata={'model': 'small'})
    auto, _ = build_score(r, 'default', instrument='Piano')
    chords, _ = build_score(r, 'default', instrument='Piano', mode='chords')
    single, _ = build_score(r, 'default', instrument='Piano', mode='simple')
    violin, _ = build_score(r, 'default', instrument='Violin')
    assert [asdict(n) for n in auto.notes] == [asdict(n) for n in chords.notes]
    assert auto.root['transcription']['mode'] == 'chords'
    def lead(s):
        return [(n.key, n.time_ms, n.duration_ms) for n in s.notes if n.extra['role'] == 'melody']
    assert lead(auto) == lead(single) == lead(violin)
    assert all(n.extra['role'] == 'melody' for n in violin.notes)
