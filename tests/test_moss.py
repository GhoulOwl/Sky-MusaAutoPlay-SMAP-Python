import pytest

from autoskyplay.transcription.events import EventResult
from autoskyplay.transcription.moss import parse_lines
from autoskyplay.transcription.rich_harmony import harmony_windows


def test_realistic_output_limits_chords_and_clips_sections():
    chords = '0.464s: C\n1.765s: Gm\n3.251s: Fmaj7\n8.916s: Dm\n'
    sections = '0.00s - 12.36s: intro\n12.36s - 48.79s: verse\n'
    result = parse_lines(chords, sections, 24000)
    assert [(c['root'], c['quality'], c['end_ms']) for c in result['chords']] == [
        (0, 'major', 1765), (7, 'minor', 3251), (2, 'minor', 12916)]
    assert result['sections'][-1]['end_ms'] == 24000
    assert parse_lines('1.0s: C7', 'unstructured answer', 24000)['raw_chords'][0]['intervals'] == [0, 4, 7, 10]
    with pytest.raises(ValueError):
        parse_lines('unstructured answer', 'unstructured answer', 24000)
    with pytest.raises(ValueError):
        parse_lines('', '0s - 5s: intro\n4s - 8s: verse', 24000)


def test_moss_resolves_only_independently_supported_candidates():
    ambiguous = [1, 0, 0, 0, 1, 0, 0, .9, 0, 1, 0, 0]
    result = EventResult([], 2000, 120, {}, {'beats_ms': [0, 500, 1000, 1500, 2000],
        'beat_confidence': .9, 'chroma': [ambiguous] * 4, 'harmony_energy_db': [-20] * 4})
    assert not harmony_windows(result, [], [])[0]
    result.analysis['moss_music'] = {'chords': [{'start_ms': 0, 'end_ms': 2000, 'root': 0, 'quality': 'major'}], 'sections': []}
    chords = harmony_windows(result, [], [(500, 1000)])[0]
    assert [(x['start_ms'], x['end_ms'], x['root']) for x in chords] == [(0, 500, 0), (1000, 1500, 0), (1500, 2000, 0)]
    assert all(x['moss_corroborated'] for x in chords)
    result.analysis['moss_music']['chords'][0]['root'] = 6
    assert not harmony_windows(result, [], [])[0]
    result.analysis['chroma'] = [[0] * 12] * 4
    result.analysis['moss_music']['chords'][0]['root'] = 0
    assert not harmony_windows(result, [], [])[0]


def test_saved_moss_data_is_validated(tmp_path):
    result = EventResult([], 1000, analysis={'moss_music': {'chords': [], 'sections': [
        {'start_ms': 0, 'end_ms': 1200, 'label': 'verse'}]}})
    with pytest.raises(ValueError):
        result.save(tmp_path / 'events.json')


def test_runner_cuts_selected_audio_and_reads_backend_output(tmp_path):
    import os
    import threading

    import numpy as np
    import soundfile as sf

    from autoskyplay.transcription.moss import MossTask

    if os.name == 'nt':
        pytest.skip('fixture executable uses a Unix shebang')
    models = tmp_path / 'models'
    models.mkdir()
    (models / 'moss-lm-q4_k_m.gguf').touch()
    (models / 'moss-aud-f16.gguf').touch()
    audio = tmp_path / 'audio.wav'
    sf.write(audio, np.zeros(48000), 16000)
    exe = tmp_path / 'ace-caption'
    exe.write_text('''#!/usr/bin/env python3
import json, sys, wave
args = sys.argv
with wave.open(args[args.index('--src-audio') + 1]) as f:
    assert f.getnframes() == 8000
out = args[args.index('-o') + 1]
with open(out.replace('.txt', '.prose.txt'), 'w') as f:
    f.write('0.0s: C\\n')
with open(out.replace('.txt', '.mm3.txt'), 'w') as f:
    f.write('0.0s - 0.5s: intro\\n')
''')
    exe.chmod(0o755)
    done = threading.Event()
    received = []
    def callback(result, error):
        received.append((result, error))
        done.set()
    task = MossTask(exe, models, audio, 1, .5, callback)
    task.thread.start()
    assert done.wait(10)
    assert received[0][1] == ''
    assert received[0][0]['sections'][0]['end_ms'] == 500


def test_sections_persist_and_edited_draft_forks(qtbot, tmp_path, monkeypatch):
    from test_transcription_ui import window_and_dialog

    from autoskyplay.netease import NeteaseSong
    from autoskyplay.transcription.events import ModelNote

    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    dialog.song = NeteaseSong('1', 'Example', '', '', '')
    dialog.result = EventResult([ModelNote(72, 0, 500, 'voice', 0)], 1000)
    dialog.remap()
    old = dialog.draft_id
    dialog.edited = True
    assert dialog.persist_draft()
    result = {'chords': [], 'sections': [{'start_ms': 0, 'end_ms': 1000, 'label': 'verse'}]}
    dialog.moss_complete(result, '')
    assert dialog.draft_id != old
    assert dialog.moss_sections.count() == 2
    assert dialog.drafts.load(dialog.draft_id)[2].analysis['moss_music'] == result
    assert 'moss_music' not in dialog.drafts.load(old)[2].analysis
    dialog.close()
    window.close()


def test_supported_moss_candidate_can_add_accompaniment_without_changing_melody():
    from autoskyplay.transcription.arrangement import build_score
    from autoskyplay.transcription.events import ModelNote

    ambiguous = [1, 0, 0, 0, 1, 0, 0, .9, 0, 1, 0, 0]
    result = EventResult([ModelNote(74, i * 500, 400, 'voice', i) for i in range(4)], 2000, 120, {}, {
        'beats_ms': [0, 500, 1000, 1500, 2000], 'beat_confidence': .9,
        'chroma': [ambiguous] * 4, 'harmony_energy_db': [-20] * 4, 'energy_db': [-20] * 20})
    baseline, _ = build_score(result, 'baseline', mode='rich_chords', shift=0, rhythm='original')
    result.analysis['moss_music'] = {'chords': [{'start_ms': 0, 'end_ms': 2000, 'root': 0, 'quality': 'major'}], 'sections': []}
    assisted, _ = build_score(result, 'assisted', mode='rich_chords', shift=0, rhythm='original')
    def lead(score):
        return [(n.key, n.time_ms, n.duration_ms) for n in score.notes if n.extra['role'] == 'melody']
    assert lead(assisted) == lead(baseline)
    assert sum(n.extra['role'] == 'accompaniment' for n in assisted.notes) > sum(
        n.extra['role'] == 'accompaniment' for n in baseline.notes)
    assert assisted.root['transcription']['chords'][0]['moss_corroborated']


def test_cancelled_worker_does_not_publish_results(tmp_path):
    import os
    import threading
    import time

    import numpy as np
    import soundfile as sf

    from autoskyplay.transcription.moss import MossTask

    if os.name == 'nt':
        pytest.skip('fixture executable uses a Unix shebang')
    (tmp_path / 'moss-lm-q4_k_m.gguf').touch()
    (tmp_path / 'moss-aud-f16.gguf').touch()
    audio = tmp_path / 'audio.wav'
    sf.write(audio, np.zeros(8000), 16000)
    exe = tmp_path / 'ace-caption'
    exe.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n')
    exe.chmod(0o755)
    done, received = threading.Event(), []
    def callback(result, error):
        received.append((result, error))
        done.set()
    task = MossTask(exe, tmp_path, audio, 0, .5, callback)
    task.thread.start()
    deadline = time.monotonic() + 5
    while task.process is None and time.monotonic() < deadline:
        time.sleep(.01)
    task.cancel()
    assert done.wait(5)
    assert received[0][0] is None and '取消' in received[0][1]


def test_conversion_spools_tensors_without_changing_values(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    converter = tmp_path / 'converter.py'
    converter.write_text('''import numpy as np
class Bundle:
    def __init__(self): self.tensors = []
    def put(self, name, arr, policy): self.tensors.append((name, arr.copy(), arr.shape, policy))
def main():
    b = Bundle()
    b.put("weight", np.array([1, 2, 3], dtype=np.float16), "f16")
    name, data, shape, policy = b.tensors[0]
    assert isinstance(data, np.memmap)
    assert data.tolist() == [1, 2, 3] and shape == (3,) and policy == "f16"
''')
    helper = Path(__file__).resolve().parents[1] / 'scripts' / 'stream_moss_convert.py'
    subprocess.run([sys.executable, str(helper), str(converter), str(tmp_path), str(tmp_path)], check=True)
    assert not list(tmp_path.glob('.tensor-spool-*'))


def test_pretranscription_section_selects_source_time(qtbot, tmp_path, monkeypatch):
    from test_transcription_ui import window_and_dialog

    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    dialog.wave_data = {'origin': 60, 'duration': 30}
    dialog.source_moss_origin = 65
    data = {'chords': [], 'sections': [{'start_ms': 1000, 'end_ms': 6000, 'label': 'verse'}]}
    dialog.moss_complete(data, '')
    dialog.moss_sections.setCurrentIndex(1)
    assert dialog.start_time.value() == 66
    assert dialog.end_time.value() == 71
    assert dialog.result is None
    dialog.discard_result()
    assert dialog.source_moss is None and dialog.moss_sections.count() == 1
    dialog.close()
    window.close()


def test_direct_output_keeps_extended_chords_bass_and_reported_boundaries():
    data = parse_lines('0s: Cmaj7\n2s: Bbm7b5/Ab\n5s: Gdim\n7s: Cweird\n9s: N.C.\n', '', 12000)
    raw = data['raw_chords']
    assert [c['symbol'] for c in raw] == ['Cmaj7', 'Bbm7b5/Ab', 'Gdim', 'Cweird']
    assert raw[0]['intervals'] == [0, 4, 7, 11]
    assert raw[1]['root'] == 10 and raw[1]['bass'] == 8 and raw[1]['intervals'] == [0, 3, 6, 10]
    assert raw[2]['intervals'] == [0, 3, 6]
    assert raw[3]['intervals'] is None and raw[3]['end_ms'] == 9000
    final = parse_lines('1s: C\n', '', 12000)
    assert final['chords'][0]['end_ms'] == 5000
    assert final['raw_chords'][0]['end_ms'] == 12000 and final['raw_chords'][0]['inferred_end']


def test_direct_mode_realizes_model_chord_without_local_evidence_or_three_key_limit():
    from autoskyplay.transcription.arrangement import build_score
    from autoskyplay.transcription.events import ModelNote

    analysis = parse_lines('0.2s: Cmaj7\n2s: F#7\n', '', 4000)
    result = EventResult([ModelNote(74, 0, 4000, 'voice', 0)], 4000, 120, {},
                         {'energy_db': [-20]*40, 'moss_music': analysis})
    baseline, _ = build_score(result, 'lead', mode='melody', shift=0, rhythm='original')
    direct, _ = build_score(result, 'direct', mode='moss_direct', shift=0, rhythm='original')
    def lead(score):
        return [(n.key, n.time_ms, n.duration_ms) for n in score.notes if n.extra['role'] == 'melody']
    assert lead(baseline) and lead(direct) == lead(baseline)
    first = [n for n in direct.notes if n.extra.get('source') == 'moss' and n.time_ms == 200]
    assert len(first) == 4 and {n.extra['harmonySymbol'] for n in first} == {'Cmaj7'}
    assert any(n.extra['approximated'] for n in direct.notes if n.extra.get('source') == 'moss')
    assert max(sum(n.time_ms <= t < n.time_ms+n.duration_ms for n in direct.notes)
               for t in (200, 2000)) > 3
    assert direct.root['transcription']['moss_direct']['last_end_inferred']


def test_direct_mode_reports_unsupported_and_legacy_chords():
    from autoskyplay.transcription.arrangement import build_score
    from autoskyplay.transcription.events import ModelNote

    result = EventResult([ModelNote(74, 0, 500, 'voice', 0)], 3000, 120, {},
                         {'energy_db': [-20]*30, 'moss_music': parse_lines('0s: Cweird\n', '', 3000)})
    direct, _ = build_score(result, 'unknown', mode='moss_direct', shift=0, rhythm='original')
    assert direct.root['transcription']['moss_direct']['unsupported'][0]['symbol'] == 'Cweird'
    assert not any(n.extra.get('source') == 'moss' for n in direct.notes)
    result.analysis['moss_music'] = {'chords': [{'start_ms': 0, 'end_ms': 1000, 'root': 0, 'quality': 'major'}],
                                     'sections': []}
    legacy, _ = build_score(result, 'legacy', mode='moss_direct', shift=0, rhythm='original')
    assert any(n.extra.get('source') == 'moss' for n in legacy.notes)
    assert any('旧版' in warning for warning in legacy.root['transcription']['warnings'])


def test_direct_mode_reports_melody_key_overlap_without_filtering():
    from autoskyplay.transcription.arrangement import build_score
    from autoskyplay.transcription.events import ModelNote

    result = EventResult([ModelNote(64, 0, 2000, 'voice', 0)], 3000, 120, {},
                         {'energy_db': [-20]*30, 'moss_music': parse_lines('0.5s: C\n', '', 3000)})
    score, _ = build_score(result, 'overlap', mode='moss_direct', shift=0, rhythm='original')
    assert score.root['transcription']['moss_direct']['melody_key_overlaps'] >= 1
    assert any(n.extra.get('source') == 'moss' and n.key == 2 for n in score.notes)


def test_direct_mode_ui_and_draft_round_trip(qtbot, tmp_path, monkeypatch):
    from test_transcription_ui import window_and_dialog

    from autoskyplay.netease import NeteaseSong
    from autoskyplay.transcription.events import ModelNote

    window, dialog = window_and_dialog(qtbot, tmp_path, monkeypatch)
    dialog.song = NeteaseSong('1', 'Direct', '', '', '')
    analysis = parse_lines('0s: Cmaj7\n2s: Gm7/Bb\n', '', 4000)
    dialog.result = EventResult([ModelNote(76, 0, 500, 'voice', 0)], 4000, 120, {},
                                {'energy_db': [-20]*40, 'moss_music': analysis})
    dialog.generation_mode.setCurrentIndex(dialog.generation_mode.findData('moss_direct'))
    dialog.flush_remap()
    assert dialog.score.root['transcription']['mode'] == 'moss_direct'
    assert any(n.extra.get('source') == 'moss' for n in dialog.score.notes)
    data, _, result, score = dialog.drafts.load(dialog.draft_id)
    assert data['params']['mode'] == 'moss_direct'
    assert result.analysis['moss_music']['raw_chords'] == analysis['raw_chords']
    assert score.root['transcription']['moss_direct']['chords'] == analysis['raw_chords']
    dialog.close()
    window.close()
