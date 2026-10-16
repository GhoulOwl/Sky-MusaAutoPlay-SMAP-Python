import hashlib
import io
import json
import threading
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
import soundfile as sf

from autoskyplay.netease import NeteaseSong
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.drafts import DraftStore
from autoskyplay.transcription.events import EventResult, ModelNote
from autoskyplay.transcription.hybrid import StageCache


def result(notes, duration=15000, voice=True):
    size = int(np.ceil(duration / 100))
    return EventResult(
        notes,
        duration,
        120,
        dict(backend="hybrid"),
        dict(
            energy_db=[-25] * size,
            vocals_energy_db=[-30 if voice else -100] * size,
            vocals_ratio=[0.5 if voice else 0] * size,
            evidence_hop_ms=100,
        ),
    )


def vocal(pitch, start, duration, index=0):
    return ModelNote(
        round(pitch), start, duration, "voice", index, "game", "vocals", f"game:vocals:{index}", pitch
    )


def test_fractional_pitch_provenance_and_separate_midi_tracks_roundtrip(tmp_path):
    a = vocal(67.23, 100, 1000)
    b = ModelNote(67, 100, 1000, "voice", 1, audio_source="background", event_id="muscriptor:background:0")
    raw = result([a, b])
    raw.save(tmp_path / "events.json")
    restored = EventResult.load(tmp_path / "events.json")
    assert restored.raw_notes == [a, b]
    assert [n.source for n in restored.notes()] == ["vocals", "other"]
    assert restored.raw_notes[0].raw_pitch_midi == 67.23
    restored.export_midi(tmp_path / "raw.mid")
    import mido

    midi = mido.MidiFile(tmp_path / "raw.mid")
    assert len(midi.tracks) == 2
    assert all(any(m.type == "text" and "rounded" in m.text for m in t) for t in midi.tracks)


def test_vocal_long_note_repeats_breath_and_background_leak():
    notes = [
        vocal(67.1, 500, 10500),
        vocal(67.1, 12000, 800, 1),
        vocal(67.1, 13200, 800, 2),
        ModelNote(67, 4000, 2000, "voice", 3, audio_source="background", event_id="muscriptor:background:3"),
        ModelNote(
            70, 11200, 400, "acoustic_piano", 4, audio_source="background", event_id="muscriptor:background:4"
        ),
    ]
    score, _ = build_score(result(notes), "long", instrument="Violin", rhythm="original")
    assert [(n.time_ms, n.duration_ms) for n in score.notes] == [(500, 10500), (12000, 800), (13200, 800)]
    assert score.notes[0].extra["rawPitchMidi"] == 67.1
    assert all(n.extra["provider"] == "game" for n in score.notes)
    assert any(d["reason"].startswith("背景分支") for d in score.root["transcription"]["event_decisions"])


def test_background_voice_label_never_treated_as_singer():
    raw = result([ModelNote(67, 0, 3000, "voice", 0, audio_source="background")], 4000, voice=False)
    score, _ = build_score(raw, "leak", instrument="Violin", rhythm="original")
    assert not score.notes
    assert any(s["needs_review"] for s in score.root["transcription"]["sections"])


def test_intro_window_boundary_does_not_split_full_instrument_note():
    raw = result(
        [ModelNote(67, 500, 10500, "flutes", 0, audio_source="background", event_id="m:0")], 12000, False
    )
    score, _ = build_score(raw, "intro", instrument="Violin", rhythm="original")
    assert [(n.time_ms, n.duration_ms) for n in score.notes] == [(500, 10500)]


def test_same_key_mapping_is_not_cross_branch_duplicate_proof():
    # Raw pitches a tritone apart may map onto a nearby Sky key, but have no
    # acoustic unison relationship. Deduplication must happen before mapping.
    raw = result(
        [
            vocal(72.1, 0, 2000),
            ModelNote(66, 100, 800, "acoustic_piano", 1, audio_source="background", event_id="m:1"),
        ],
        3000,
    )
    score, _ = build_score(raw, "support", instrument="Piano", rhythm="original")
    decisions = score.root["transcription"]["event_decisions"]
    assert any(d["event_id"] == "m:1" and d["action"] == "accompaniment_candidate" for d in decisions)
    assert not any(d["event_id"] == "m:1" and "同音／八度" in d["reason"] for d in decisions)


def test_cache_partial_corruption_required_files_and_cancel(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "events.json").write_text("{}")
    cache = StageCache(tmp_path, "game", {"input": "voice", "model": {"files": {"model.pt": (100, "fixed")}}})
    assert not cache.restore(work, ("events.json",))
    cache.publish(work, ("events.json",))
    assert cache.restore(work, ("events.json",))
    assert not cache.restore(work, ("events.json", "missing.json"))
    (cache.path / "events.json").write_text("broken")
    assert not cache.restore(work, ("events.json",))
    with cache.lock(), pytest.raises(ValueError, match="使用或管理"):
        with cache.lock():
            pass
    cancelled = StageCache(tmp_path, "game", {"input": "other"}, cancelled=lambda: True)
    with pytest.raises(ValueError, match="取消"):
        cancelled.publish(work, ("events.json",))
    assert not cancelled.path.exists()


def test_hybrid_draft_survives_derived_wav_deletion_and_model_removal(tmp_path):
    raw = result([vocal(67.4, 500, 5000)], 7000)
    score, _ = build_score(raw, "draft", instrument="Violin", rhythm="original")
    media = tmp_path / "media"
    media.mkdir()
    (media / "source.audio").write_bytes(b"original")
    sf.write(media / "background.wav", np.zeros(1000), 44100)
    store = DraftStore(tmp_path)
    ident = store.save(None, NeteaseSong("1", "Draft", "", "", ""), raw, score, {}, media=media, edited=True)
    store.prune_audio(budget=0)
    data, _, restored, kept = store.load(ident)
    assert data["edited"] and kept.to_data() == score.to_data()
    assert restored.raw_notes[0].raw_pitch_midi == 67.4
    assert (store.path(ident) / "source.audio").read_bytes() == b"original"
    assert not (store.path(ident) / "background.wav").exists()
    regenerated, _ = build_score(restored, "again", instrument="Piano")
    assert regenerated.notes


def test_schema_one_legacy_event_still_loads(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps(
            dict(
                schema=1,
                notes=[dict(pitch=67, start_ms=0, duration_ms=1000, instrument="voice", index=1)],
                duration_ms=2000,
                bpm=120,
            )
        )
    )
    old = EventResult.load(path)
    assert old.notes()[0].source == "vocals"
    invalid = replace(old.raw_notes[0], event_id="id", raw_pitch_midi=float("nan"))
    with pytest.raises(ValueError):
        result([invalid]).validate()


@pytest.mark.parametrize("range_supported", [True, False])
def test_game_release_resume_pinned_members_and_delete_keeps_draft(tmp_path, monkeypatch, range_supported):
    from autoskyplay.transcription import game_models as gm

    payload = {"model.pt": b"weights", "config.yaml": b"model: test", "lang_map.json": b'{"zh": 4}'}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, value in payload.items():
            package.writestr("GAME-1.0-medium/" + name, value)
        package.writestr("../../outside.txt", b"never extract")
    archive = buffer.getvalue()
    monkeypatch.setattr(gm, "GAME_ARCHIVE_SIZE", len(archive))
    monkeypatch.setattr(gm, "GAME_ARCHIVE_SHA", hashlib.sha256(archive).hexdigest())
    monkeypatch.setitem(gm.GAME_MODELS, "medium", dict(revision="test", files={
        name: (len(value), hashlib.sha256(value).hexdigest()) for name, value in payload.items()}))
    store = gm.GameModelStore(tmp_path)
    stage = store.root / ".game-medium-download"
    stage.mkdir(parents=True)
    (stage / "release.zip").write_bytes(archive[:13])

    def reply(request):
        assert request.headers["range"] == "bytes=13-"
        if range_supported:
            return httpx.Response(206, content=archive[13:], headers={
                "Content-Range": f"bytes 13-{len(archive)-1}/{len(archive)}"})
        return httpx.Response(200, content=archive)

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        store.install(client=client)
    assert store.verify("medium").read_bytes() == payload["model.pt"]
    assert not (store.root.parent / "outside.txt").exists() and not stage.exists()
    draft = tmp_path / "transcription/drafts/source.audio"
    draft.parent.mkdir()
    draft.write_bytes(b"durable")
    with store.lock("medium"), pytest.raises(ValueError, match="使用或管理"):
        store.delete()
    store.delete()
    assert not store.installed("medium") and draft.read_bytes() == b"durable"


def test_game_long_phrase_advances_inside_rest_never_on_held_note():
    from autoskyplay.transcription.game_worker import rest_cut

    regions = [
        dict(onset_ms=0, offset_ms=10500, voiced=True),
        dict(onset_ms=10500, offset_ms=10800, voiced=False),
        dict(onset_ms=10800, offset_ms=32000, voiced=True),
        dict(onset_ms=32000, offset_ms=34000, voiced=False),
    ]
    assert rest_cut(regions, 0, 35000) == 10650  # final context-edge rest is excluded
    assert rest_cut([dict(onset_ms=0, offset_ms=45000, voiced=True)], 0, 45000) is None
    assert rest_cut([dict(onset_ms=12000, offset_ms=12030, voiced=False)], 0, 35000) is None


def test_numerical_tail_keeps_quiet_sustain_interior_rests_and_quiet_recordings():
    from autoskyplay.transcription.background_input import numerical_tail

    rate = 16000
    t = np.arange(8 * rate) / rate
    held = 0.1 * np.sin(2 * np.pi * 440 * t)
    held[2 * rate : 3 * rate] = 0
    original = held.copy()
    kept, _ = numerical_tail(held)
    assert len(kept) == len(held) and np.array_equal(held, original)
    quiet, _ = numerical_tail(held * 1e-8)
    assert len(quiet) == len(held)
    tail = held.copy()
    tail[6 * rate :] = 1e-7
    kept, info = numerical_tail(tail)
    assert len(kept) == round(6.2 * rate) and info["omitted_tail_ms"] == 1800
    tail = held.copy()
    tail[6 * rate :] = held[6 * rate :] * 0.001
    assert len(numerical_tail(tail)[0]) == len(held)


def test_audio_identity_ignores_wav_peak_timestamp_but_detects_samples(tmp_path):
    import struct

    from autoskyplay.transcription.hybrid import audio_digest
    from autoskyplay.transcription.models import digest

    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(a, np.ones((1000, 2), np.float32) * 0.1, 44100, subtype="FLOAT")
    raw = bytearray(a.read_bytes())
    offset = raw.index(b"PEAK") + 12
    raw[offset : offset + 4] = struct.pack("<I", 1)
    b.write_bytes(raw)
    assert digest(a) != digest(b) and audio_digest(a) == audio_digest(b)
    sf.write(b, np.ones((1000, 2), np.float32) * 0.2, 44100, subtype="FLOAT")
    assert audio_digest(a) != audio_digest(b)


@pytest.mark.parametrize(
    "kind,suspicious", [("held", True), ("gradual", True), ("reattack", False), ("timbre", False)]
)
def test_native_acoustic_advice_never_merges_real_or_suspected_boundaries(tmp_path, kind, suspicious):
    from test_transcription_sustain import acoustic_result

    from autoskyplay.transcription.hybrid_evidence import annotate_vocal_boundaries

    _, path = acoustic_result(tmp_path, attacks=(1000,), kind=kind)
    raw = result([vocal(69, 0, 1000), vocal(69, 1000, 1000, 1)], 2000)
    before = [(n.start_ms, n.duration_ms, n.pitch) for n in raw.raw_notes]
    annotate_vocal_boundaries(raw, path)
    assert before == [(n.start_ms, n.duration_ms, n.pitch) for n in raw.raw_notes]
    assert raw.analysis["boundary_reviews"][0]["needs_review"] is suspicious


def test_stem_eviction_protects_live_entry_and_keeps_raw_events_and_models(tmp_path):
    from autoskyplay.transcription.hybrid import STEMS, clear_hybrid_audio, prune_hybrid_audio

    work = tmp_path / "work"
    work.mkdir()
    for name in STEMS:
        (work / name).write_bytes(b"float stem")
    first = StageCache(tmp_path, "separation", {"source": 1})
    second = StageCache(tmp_path, "separation", {"source": 2})
    first.publish(work, STEMS)
    second.publish(work, STEMS)
    event = tmp_path / "transcription/hybrid-cache/game/saved/events.json"
    model = tmp_path / "transcription/models/fixed/model.pt"
    source = tmp_path / "transcription/drafts/saved/source.audio"
    for path in (event, model, source):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"keep")
    prune_hybrid_audio(tmp_path, budget=0, protected={second.key})
    assert not (first.path / "vocals.wav").exists() and (second.path / "vocals.wav").exists()
    clear_hybrid_audio(tmp_path)
    assert not (second.path / "vocals.wav").exists()
    assert all(path.read_bytes() == b"keep" for path in (event, model, source))


@pytest.mark.parametrize('accelerated', [True, False])
def test_hybrid_propagates_device_to_all_stages_and_separates_caches(tmp_path, monkeypatch, accelerated):
    from autoskyplay.transcription.engine import Engine
    from autoskyplay.transcription.hybrid import STEMS, run_hybrid

    work = tmp_path/'work'
    work.mkdir()
    (work/'source.audio').write_bytes(b'source')
    (work/'request.json').write_text(json.dumps(dict(start=0, length=1)))
    models = tmp_path/'models'
    models.mkdir()
    (models/'models.json').write_text('{}')
    calls = []

    def run(engine, folder, extra=()):
        stage = engine.command[0]
        calls.append((stage, engine.device))
        if stage == 'separate':
            for name in STEMS:
                sf.write(folder/name, np.zeros((44100, 2)), 44100, subtype='FLOAT')
            (folder/'separation.json').write_text('{}')
        elif stage == 'features':
            (folder/'features.json').write_text(json.dumps(dict(duration_ms=1000, bpm=120,
                                                               bpm_estimated=False, analysis={})))
        else:
            EventResult([], 1000, 120, dict(device=engine.device), {}).save(folder/'events.json')
        return dict(device=engine.device)

    monkeypatch.setattr('autoskyplay.transcription.hybrid_evidence.annotate_vocal_boundaries', lambda *args: None)
    task = SimpleNamespace(
        directory=tmp_path, work=work, cancelled=threading.Event(), check_cancelled=lambda: None,
        emit=lambda *args, **kwargs: None, _run_engine=run,
        engine=Engine([], models, 'test', 'hybrid', device='auto',
                      config=dict(commands={s: [s] for s in ('separate', 'game', 'background', 'features')},
                                  separation_models=str(models), accelerated_stages=accelerated)))
    first = run_hybrid(task)
    expected = 'auto' if accelerated else 'cpu'
    assert ('separate', expected) in calls and ('game', expected) in calls and ('background', 'auto') in calls
    calls.clear()
    assert all(run_hybrid(task).metadata['cache_hits'].values())
    assert not calls
    task.engine = replace(task.engine, device='cpu')
    second = run_hybrid(task)
    assert ('background', 'cpu') in calls
    assert not second.metadata['cache_hits']['background']
    for stage in ('separation', 'game'):
        assert second.metadata['cache_hits'][stage] == (not accelerated)
        assert first.metadata['cache_identities'][stage]['identity']['device'] == expected
    calls.clear()
    task.engine.config['game_device'] = 'cuda'
    third = run_hybrid(task)
    assert calls == [('game', 'cuda')]
    assert third.metadata['cache_identities']['game']['identity']['device'] == 'cuda'
