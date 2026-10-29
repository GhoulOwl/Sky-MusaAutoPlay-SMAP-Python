import copy
import json
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.drafts import AUDIO_FILES
from autoskyplay.transcription.engine import Engine
from autoskyplay.transcription.events import EventResult, ModelNote
from autoskyplay.transcription.multistem import PITCHED_STEMS, SEPARATED_STEMS, run_multistem
from autoskyplay.transcription.song_analysis import edit_rhythm, validate_beats


def raw_result():
    beats = [float(v) for v in range(0, 4001, 500)]
    notes = [ModelNote(69, 500, 2000, 'voice', 0, provider='game', audio_source='lead_vocals', event_id='lead:0'),
             ModelNote(60, 500, 500, 'acoustic_piano', 1, audio_source='piano', event_id='piano:0')]
    return EventResult(notes, 4500, 120, dict(backend='hybrid', pipeline='multistem'),
                       dict(beats_ms=beats, beat_confidence=.8, chroma=[[1.] * 12] * 8,
                            harmony_energy_db=[-20.] * 8, energy_db=[-20.] * 45,
                            vocals_energy_db=[-20.] * 45, vocals_ratio=[.9] * 45))


def test_lead_is_vocal_and_excluded_sources_can_never_be_committed():
    raw = raw_result()
    assert raw.raw_notes[0].source == 'vocals'
    score, _ = build_score(raw, 'lead', instrument='Violin', rhythm='original')
    assert [(n.time_ms, n.duration_ms) for n in score.notes] == [(500, 2000)]
    for excluded in ('backing_vocals', 'drums', 'vocals', 'background'):
        invalid = copy.deepcopy(raw)
        invalid.raw_notes.append(replace(raw.raw_notes[0], index=2, event_id='excluded', audio_source=excluded))
        with pytest.raises(ValueError, match='排除'):
            invalid.validate()


@pytest.mark.parametrize('beats', [[0, 500, 500, 1000], [0, float('nan')], [-1, 500], [0, 5000], [[1, 2]]])
def test_invalid_beat_edits_are_rejected_without_touching_original(beats):
    original = raw_result()
    snapshot = copy.deepcopy(original)
    with pytest.raises(ValueError):
        edit_rhythm(original, beats)
    assert original.raw_notes == snapshot.raw_notes and original.analysis == snapshot.analysis


def test_manual_rhythm_regrids_evidence_and_preserves_raw_events():
    original = raw_result()
    original.analysis['chroma'] = [[float(i)] * 12 for i in range(8)]
    changed = edit_rhythm(original, [0, 1000, 2000, 3000, 4000])
    assert changed.bpm == 60 and original.bpm == 120
    assert changed.raw_notes == original.raw_notes
    assert [r[0] for r in changed.analysis['chroma']] == [.5, 2.5, 4.5, 6.5]
    assert changed.analysis['rhythm_original']['beats_ms'] == original.analysis['beats_ms']
    again = edit_rhythm(changed, [0, 500, 1000, 1500, 2000, 2500, 3000, 3500, 4000])
    assert again.analysis['chroma'] == original.analysis['chroma']
    assert set(AUDIO_FILES) >= {'lead_vocals.wav', 'guitar.wav', 'piano.wav'}


def fake_pipeline(tmp_path, monkeypatch):
    work, models = tmp_path / 'work', tmp_path / 'models'
    work.mkdir()
    models.mkdir()
    (work / 'source.audio').write_bytes(b'original source')
    (work / 'request.json').write_text(json.dumps(dict(start=40, length=1)))
    calls, inputs = [], {}
    fail = set()

    def run(engine, folder, extra=()):
        stage = engine.command[0]
        calls.append((stage, folder.name))
        if stage == 'separate':
            assert '--separation-model' in extra and 'six' in extra
            for i, name in enumerate(SEPARATED_STEMS):
                sf.write(folder / f'{name}.wav', np.full((44100, 2), (i + 1) / 100), 44100, subtype='FLOAT')
            (folder / 'separation.json').write_text('{}')
        elif stage == 'vocal_split':
            for name, value in [('lead_vocals', .02), ('backing_vocals', .08)]:
                sf.write(folder / f'{name}.wav', np.full((44100, 2), value), 44100, subtype='FLOAT')
            (folder / 'vocal-split.json').write_text('{}')
        elif stage == 'features':
            (folder / 'features.json').write_text(json.dumps(dict(duration_ms=1000, bpm=120,
                  bpm_estimated=True, analysis={}, rhythm_analysis={}, key_analysis={})))
        else:
            req = json.loads((folder / 'request.json').read_text())
            source = req['audio_source']
            wave, _ = sf.read(folder / ('vocals.wav' if stage == 'game' else 'source.audio'))
            inputs[source] = float(wave[0, 0])
            if source in fail:
                raise ValueError('simulated interrupted inference')
            note = ModelNote(69, 100, 300, 'voice' if stage == 'game' else 'acoustic_piano', 0,
                             provider='game' if stage == 'game' else 'muscriptor', audio_source=source,
                             event_id=f'{source}:0')
            EventResult([note], 1000, metadata=dict(device=engine.device)).save(folder / 'events.json')
        return dict(device=engine.device)

    monkeypatch.setattr('autoskyplay.transcription.hybrid_evidence.annotate_vocal_boundaries', lambda *a: None)
    task = SimpleNamespace(directory=tmp_path, work=work, cancelled=threading.Event(),
             check_cancelled=lambda: None, emit=lambda *a, **k: None, _run_engine=run,
             audio=SimpleNamespace(is_trial=False, available_seconds=180), start_seconds=40,
             engine=Engine([], models, '4.0.0', 'hybrid', device='cpu', config=dict(multistem=True,
                 commands={n: [n] for n in ('separate', 'game', 'background', 'features', 'vocal_split')},
                 separation_models=str(models))))
    return task, calls, inputs, fail


def test_separate_inputs_cache_reuse_and_exact_selection_origin(tmp_path, monkeypatch):
    task, calls, inputs, _ = fake_pipeline(tmp_path, monkeypatch)
    result = run_multistem(task)
    assert set(inputs) == {'lead_vocals', *PITCHED_STEMS}
    assert inputs['lead_vocals'] == pytest.approx(.02)
    assert len(set(inputs[n] for n in PITCHED_STEMS)) == 4
    assert len(result.raw_notes) == 5 and len({n.identity for n in result.raw_notes}) == 5
    assert not json.loads((task.work / 'analysis-request.json').read_text())['ends_song']
    for name in PITCHED_STEMS:
        request = json.loads((task.work / name / 'request.json').read_text())
        assert request['start'] == 0 and request['input_origin_seconds'] == 40
    calls.clear()
    assert all(run_multistem(task).metadata['cache_hits'].values())
    assert not calls
    task.engine.config['game'] = dict(steps=16)
    run_multistem(task)
    assert calls == [('game', 'game')]


def test_failed_branch_is_not_published_and_resume_reuses_completed_branches(tmp_path, monkeypatch):
    task, calls, _, fail = fake_pipeline(tmp_path, monkeypatch)
    fail.add('piano')
    with pytest.raises(ValueError, match='interrupted'):
        run_multistem(task)
    assert not (task.work / 'events.json').exists()
    assert not list((tmp_path / 'transcription/hybrid-cache/instrument-piano').glob('*/cache.json'))
    calls.clear()
    fail.clear()
    result = run_multistem(task)
    assert calls == [('background', 'piano'), ('background', 'other')]
    assert result.metadata['cache_hits']['instrument-bass']


def test_rhythm_editor_rejects_invalid_and_cancels_without_mutation(qtbot):
    from autoskyplay.ui.rhythm_editor import RhythmEditor
    original = raw_result()
    dialog = RhythmEditor(original)
    qtbot.addWidget(dialog)
    dialog.double()
    assert len(dialog.beats()) == 17
    dialog.half()
    assert dialog.beats() == original.analysis['beats_ms']
    dialog.table.item(1, 0).setText('invalid')
    dialog.apply()
    assert dialog.result is None and dialog.error.text()
    dialog.reset()
    dialog.half()
    dialog.apply()
    assert dialog.result.bpm == 60 and original.bpm == 120
    assert dialog.result.raw_notes == original.raw_notes


def test_validate_beat_bounds():
    assert validate_beats([0, 500, 1000, 1500], 2000).tolist() == [0, 500, 1000, 1500]


def test_neural_half_time_disagreement_requires_review_and_missing_model_falls_back(tmp_path, monkeypatch):
    from autoskyplay.transcription import song_analysis as sa

    beats = np.arange(0, 8000, 500, dtype=float)
    acoustic = dict(beats_ms=beats, beat_confidence=np.array(.9), chroma=np.ones((15, 12)),
                    harmony_energy_db=np.full(15, -20.))
    monkeypatch.setattr(sa, 'rhythm_harmony', lambda *a: (copy.deepcopy(acoustic), 120., True))
    monkeypatch.setattr(sa, 'refine_beats', lambda y, sr, b: (np.asarray(b), 0))
    monkeypatch.setattr(sa, 'harmony_at_beats', lambda y, sr, b: dict(
        chroma=np.ones((len(b)-1, 12)), harmony_energy_db=np.full(len(b)-1, -20.)))
    model = tmp_path / 'local.ckpt'
    model.write_bytes(b'model')
    monkeypatch.setattr(sa, 'verify_asset', lambda *a: model)

    def predictor(**kwargs):
        assert hasattr(kwargs['checkpoint_path'], 'read') and kwargs['dbn'] is False
        return lambda y, sr: (np.arange(8, dtype=float), np.array([0., 4.]))

    monkeypatch.setitem(sys.modules, 'beat_this.inference', SimpleNamespace(Audio2Beats=predictor))
    wave = np.ones((8000, 2), dtype=np.float32)
    analysis, bpm, valid, receipt = sa.detect_rhythm(wave, wave, wave, 1000, tmp_path)
    assert valid and bpm == 60 and receipt['half_double_conflict']
    assert analysis['beat_confidence'] <= .5 and receipt['extrapolated_beats'] == 0
    model.unlink()
    analysis, bpm, valid, receipt = sa.detect_rhythm(wave, wave, wave, 1000, tmp_path)
    assert receipt['detector'] == 'librosa' and receipt['fallback_reason']
    assert bpm == 120 and analysis['beats_ms'].tolist() == beats.tolist()
    with pytest.raises(ValueError, match='不可用'):
        sa.detect_rhythm(wave, wave, wave, 1000, tmp_path, detector='model')


def test_cross_stem_doubling_does_not_manufacture_chord_votes():
    from autoskyplay.transcription.melody import MelodyNote
    from autoskyplay.transcription.rich_harmony import harmony_windows

    result = EventResult([], 1000, metadata=dict(pipeline='multistem'))
    chord = [MelodyNote(p, 0, 1000, None, role='accompaniment', event_id=str(p), audio_source='piano')
             for p in (60, 64, 67)]
    before = harmony_windows(result, chord, [])[0]
    doubled = chord + [replace(chord[0], event_id=f'double:{i}', audio_source='guitar') for i in range(10)]
    after = harmony_windows(result, doubled, [])[0]
    assert before and after and before[0]['root'] == after[0]['root'] == 0
    assert before[0]['quality'] == after[0]['quality'] == 'major'


def test_quiet_other_track_keeps_raw_events_but_supplies_no_harmony():
    from autoskyplay.transcription.rich_harmony import evidence_notes
    raw = raw_result()
    raw.raw_notes[1] = replace(raw.raw_notes[1], audio_source='other')
    raw.analysis['stem_energy_db'] = dict(other=[-100.] * 45)
    assert not evidence_notes(raw, 80)
    assert len(raw.raw_notes) == 2
    raw.analysis['stem_energy_db']['other'] = [-20.] * 45
    assert len(evidence_notes(raw, 80)) == 1


def test_other_only_instrumental_theme_is_preserved():
    raw = EventResult([ModelNote(69, 500, 3000, 'strings', 0, audio_source='other', event_id='other:0')],
                      4500, 120, dict(backend='hybrid', pipeline='multistem'),
                      dict(energy_db=[-20.] * 45, vocals_energy_db=[-100.] * 45,
                           vocals_ratio=[0.] * 45, stem_energy_db=dict(other=[-20.] * 45)))
    score, _ = build_score(raw, 'other theme', instrument='Violin', rhythm='original')
    assert [(n.time_ms, n.duration_ms) for n in score.notes] == [(500, 3000)]
    assert score.notes[0].extra['audioSource'] == 'other'


def test_v4_component_requires_fixed_assets_and_selects_new_pipeline(tmp_path, monkeypatch):
    import hashlib
    import zipfile

    from autoskyplay.transcription import multistem_models
    from autoskyplay.transcription.engine import install_engine, platform_tag

    assets = {name: dict(filename=f'{name}.bin', sha256=hashlib.sha256(name.encode()).hexdigest())
              for name in ('demucs', 'karaoke', 'beat')}
    monkeypatch.setattr(multistem_models, 'ASSETS', assets)
    files = {'engine': b'entrypoint', 'runtime.json': b'{}', 'models/models.json': b'{}'}
    archive = tmp_path / 'component.zip'

    def pack():
        manifest = dict(schema=1, version='4.0.0', platform=platform_tag(), entry='engine',
                        files={n: hashlib.sha256(v).hexdigest() for n, v in files.items()})
        with zipfile.ZipFile(archive, 'w') as package:
            package.writestr('engine.json', json.dumps(manifest))
            for name, payload in files.items():
                package.writestr(name, payload)

    pack()
    with pytest.raises(ValueError, match='固定模型'):
        install_engine(archive, tmp_path / 'data')
    for name, asset in assets.items():
        files['models/' + asset['filename']] = name.encode()
    pack()
    engine = install_engine(archive, tmp_path / 'data')
    assert engine.config['multistem'] and 'vocal_split' in engine.config['commands']


def test_v4_device_summary_does_not_claim_karaoke_uses_torch_gpu(qtbot):
    from autoskyplay.ui.device_status import DeviceStatus
    widget = DeviceStatus()
    qtbot.addWidget(widget)
    widget.multistem = True
    report = dict(cpu=dict(name='test CPU', logical_cores=4),
                  gpus=[dict(name='test GPU', device='cuda', usable=True)],
                  stages=dict(separate='cuda', game='cuda', background='cuda'))
    widget.reports = {s: report for s in ('separate', 'game', 'background')}
    widget.show_reports()
    assert '去和声 CPU' in widget.summary.text() and '全阶段' not in widget.summary.text()
