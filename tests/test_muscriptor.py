import hashlib
import json
from dataclasses import replace

import httpx
import mido
import pytest

from autoskyplay.netease import NeteaseSong
from autoskyplay.score import Note, Score
from autoskyplay.transcription.arrangement import build_score
from autoskyplay.transcription.drafts import DraftStore
from autoskyplay.transcription.events import EventResult, ModelNote, arrange_events
from autoskyplay.transcription.models import MODELS, ModelStore


@pytest.fixture
def model_files(monkeypatch):
    files = {'model.safetensors': b'0123456789', 'config.json': b'{}'}
    monkeypatch.setitem(MODELS, 'small', dict(revision='fixed', files={
        name: (len(data), hashlib.sha256(data).hexdigest()) for name, data in files.items()}))
    return files


@pytest.mark.parametrize('range_supported', [True, False])
def test_download_resumes_or_restarts_atomically(tmp_path, model_files, range_supported):
    store = ModelStore(tmp_path)
    stage = store.path('small').with_suffix('.partial')
    stage.mkdir(parents=True)
    (stage/'model.safetensors').write_bytes(b'0123')
    calls = []

    def handler(request):
        name = request.url.params['FilePath']
        calls.append((name, request.headers.get('range')))
        assert request.url.host == 'modelscope.cn' and request.url.params['Revision'] == 'fixed'
        if range_supported and name == 'model.safetensors':
            return httpx.Response(206, content=model_files[name][4:], headers={'Content-Range': 'bytes 4-9/10'})
        return httpx.Response(200, content=model_files[name])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        store.install('small', client=client)
    assert calls[0] == ('model.safetensors', 'bytes=4-')
    assert store.installed('small')
    assert store.verify('small').read_bytes() == model_files['model.safetensors']
    assert not stage.exists()


def test_corruption_cancel_and_locks_never_publish(tmp_path, model_files):
    store = ModelStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'XXXXXXXXXX'))) as client:
        with pytest.raises(ValueError, match='校验失败'):
            store.install('small', client=client)
    assert not store.installed('small')
    with pytest.raises(ValueError, match='取消'):
        store.install('small', cancelled=lambda: True)
    with store.lock('small'), pytest.raises(ValueError, match='使用或管理'):
        store.delete('small')


def test_delete_model_preserves_drafts_and_source(tmp_path, model_files):
    source = tmp_path/'import'
    source.mkdir()
    for name, data in model_files.items():
        (source/name).write_bytes(data)
    store = ModelStore(tmp_path)
    store.install('small', source=source)
    draft = tmp_path/'transcription/drafts/saved/source.audio'
    draft.parent.mkdir(parents=True)
    draft.write_bytes(b'original')
    store.delete('small')
    assert not store.installed('small') and draft.read_bytes() == b'original'


def native(notes, duration=18000):
    return EventResult(notes, duration, 120, {'model': 'small'}, {'energy_db': [-30]*int(duration/100)})


def test_cross_window_long_note_real_repeats_and_breaths_survive():
    result = native([ModelNote(67, 500, 10500, 'voice', 1), ModelNote(67, 12000, 800, 'voice', 2),
                     ModelNote(67, 13200, 800, 'voice', 3), ModelNote(69, 15000, 1000, 'voice', 4)])
    score, _ = build_score(result, 'Long note', instrument='Violin', rhythm='original')
    lead = [n for n in score.notes if n.extra['role'] == 'melody']
    assert [(n.time_ms, n.duration_ms) for n in lead] == [(500,10500), (12000,800), (13200,800), (15000,1000)]
    assert all(n.extra['pitchConfidence'] is None for n in lead)
    assert all('modelEvent' in n.extra for n in lead)


def test_source_switch_cannot_cut_held_event_and_tiny_support_rejected():
    result = native([ModelNote(67, 0, 6000, 'voice', 0), ModelNote(70, 5500, 1000, 'flutes', 1),
                     ModelNote(72, 6600, 1500, 'flutes', 2), ModelNote(48, 0, 20, 'acoustic_bass', 3)])
    score, _ = build_score(result, 'Sources', instrument='Piano', rhythm='original')
    first = next(n for n in score.notes if n.extra.get('modelEvent') == 0)
    assert first.duration_ms == 6000
    assert not any(n.extra.get('modelEvent') == 3 for n in score.notes)


def test_ambiguous_sources_remain_review_rests():
    result = native([ModelNote(pitch, t, 500, instrument, i*10+j)
                     for i, instrument in enumerate(('flutes', 'violin'))
                     for j, (t, pitch) in enumerate(((0,67),(600,69),(1200,67),(1800,69)))], 3000)
    notes, info = arrange_events(result, mode='melody')
    assert not notes
    assert any(s['needs_review'] for s in info['sections'])


def test_hallucinated_events_in_silence_do_not_become_lead():
    result = native([ModelNote(67,0,2000,'voice',1)], 3000)
    result.analysis['energy_db'] = [-100]*30
    notes, info = arrange_events(result)
    assert not notes and all(s['source'] == 'rest' for s in info['sections'])


def test_model_ui_defaults_and_native_confidence_hidden(qtbot, tmp_path, monkeypatch):
    from test_transcription_ui import window_and_dialog
    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    assert dialog.model_choice.currentData() == 'small'
    assert not dialog.threshold.isEnabled()
    dialog.result = native([ModelNote(67,0,2000,'voice',1)], 3000)
    dialog.song = NeteaseSong('1','Test','','','')
    dialog.remap()
    assert dialog.score.root['transcription']['threshold'] is None
    assert not dialog.threshold.isEnabled()
    old = dialog.score.to_data()
    dialog.model_choice.setCurrentIndex(1)
    assert dialog.score.to_data() == old
    dialog.close()
    window.close()


def test_native_draft_and_multitrack_midi_keep_source_events(tmp_path):
    result = native([ModelNote(67, 100, 5900, 'voice', 1), ModelNote(48, 100, 1000, 'acoustic_piano', 2),
                     ModelNote(36, 200, 10, 'drums', 3)])
    store = DraftStore(tmp_path)
    edited = Score('Edited', notes=[Note(5, 99, 321)])
    ident = store.save(None, NeteaseSong('1','Test','','',''), result, edited, {}, edited=True)
    data, _, restored, score = store.load(ident)
    assert data['result_type'] == 'events' and data['edited']
    assert score.notes[0].duration_ms == 321 and restored.raw_notes == result.raw_notes
    result.export_midi(tmp_path/'raw.mid')
    midi = mido.MidiFile(tmp_path/'raw.mid')
    assert len(midi.tracks) == 3
    starts = [m for t in midi.tracks for m in t if m.type == 'note_on']
    assert {m.note for m in starts} == {67,48,36}
    assert next(m for m in starts if m.note == 36).channel == 9
    assert all(m.velocity == 80 for m in starts)
    assert result.raw_notes[0].duration_ms == 5900


@pytest.mark.parametrize('change', [{'pitch': 128}, {'start_ms': -1}, {'duration_ms': float('nan')}])
def test_invalid_native_events_rejected(change):
    result = native([replace(ModelNote(67, 0, 100, 'voice', 0), **change)])
    with pytest.raises(ValueError):
        result.validate()


def test_result_cache_identity_includes_model_range_and_bytes(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from autoskyplay.netease import NeteaseAudio
    from autoskyplay.transcription.engine import Engine
    from autoskyplay.transcription.task import TranscriptionTask
    calls = []
    def run(task):
        calls.append(task.engine.model)
        result = native([ModelNote(67,0,500,'voice',0)], 1000)
        result.save(task.work/'events.json')
        return result.metadata
    monkeypatch.setattr(TranscriptionTask, '_run_engine', run)
    def request(model='small', contents=b'original', start=0):
        task = TranscriptionTask(tmp_path, SimpleNamespace(), NeteaseSong('1','','','',''),
                                 NeteaseAudio('1','',10000), Engine([],tmp_path,'2.0.0','muscriptor',model), start,1)
        (task.work/'source.audio').write_bytes(contents)
        (task.work/'request.json').write_text(json.dumps(dict(start=start,length=1)))
        result=task._run_muscriptor()
        task.close()
        return result
    assert not request().metadata['cache_hit']
    assert request().metadata['cache_hit']
    request('medium')
    request(contents=b'changed')
    request(start=1)
    assert calls == ['small','medium','small','small']
