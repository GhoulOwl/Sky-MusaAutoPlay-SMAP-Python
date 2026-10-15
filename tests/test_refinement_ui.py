import copy
import json

import numpy as np
import soundfile as sf
from PySide6.QtCore import Qt
from test_refinement import fixture, local_for
from test_transcription_ui import window_and_dialog

from autoskyplay.netease import NeteaseSong
from autoskyplay.transcription.refinement import CandidateStore, Region, make_candidate, search_context
from autoskyplay.ui.refinement import RefinementDialog


def prepare(dialog, tmp_path):
    r, score, params = fixture()
    r.metadata.update(source_start=0, source_origin=0)
    dialog.song = NeteaseSong('1', 'Test', '', '', '', 10000)
    dialog.result, dialog.score = r, score
    dialog.source_dir = tmp_path/'media'
    dialog.source_dir.mkdir()
    sf.write(dialog.source_dir/'source.audio', np.zeros(160000), 16000, format='WAV')
    dialog.generation_mode.setCurrentIndex(dialog.generation_mode.findData('melody'))
    dialog.remap_timer.stop()
    assert dialog.persist_draft()
    return r, score, dialog.parameters()


def test_review_accept_is_new_draft_and_original_survives(qtbot, tmp_path, monkeypatch):
    window, parent = window_and_dialog(qtbot, tmp_path, monkeypatch)
    r, score, params = prepare(parent, tmp_path)
    region = Region.create(r, 3000, 6000)
    candidate = make_candidate(parent.draft_id, r, score, params, local_for(r, region, True), region)
    store = CandidateStore(tmp_path)
    store.save(candidate, local_for(r, region, True))
    original = copy.deepcopy(parent.score.to_data())
    review = RefinementDialog(parent)
    qtbot.addWidget(review)
    review.show()
    assert review.accept_button.isEnabled()
    assert not review.generate.isEnabled()
    review.version.setCurrentIndex(1)
    assert parent.score.to_data() == original
    review.accept_candidate()
    assert review.child_id and review.child_id != parent.draft_id
    assert parent.score.to_data() == original
    assert len(parent.drafts.entries()) == 2
    parent.close()
    window.close()


def test_pending_review_closes_and_recovers_with_locked_range(qtbot, tmp_path, monkeypatch):
    window, parent = window_and_dialog(qtbot, tmp_path, monkeypatch)
    r, score, params = prepare(parent, tmp_path)
    region = Region.create(r, 3000, 6000)
    local = local_for(r, region)
    candidate = make_candidate(parent.draft_id, r, score, params, local, region)
    CandidateStore(tmp_path).save(candidate, local)
    review = RefinementDialog(parent)
    qtbot.addWidget(review)
    review.show()
    qtbot.keyClick(review, Qt.Key.Key_Escape)
    assert not review.isVisible()
    again = RefinementDialog(parent)
    qtbot.addWidget(again)
    assert again.candidate.ident == candidate.ident
    assert again.left.value() == 3 and not again.left.isEnabled()
    again.discard()
    assert CandidateStore(tmp_path).pending(parent.draft_id) is None
    assert again.generate.isEnabled()
    again.close()
    parent.close()
    window.close()


def test_reference_range_changes_do_not_move_original_notes(qtbot, tmp_path, monkeypatch):
    window, parent = window_and_dialog(qtbot, tmp_path, monkeypatch)
    prepare(parent, tmp_path)
    review = RefinementDialog(parent)
    qtbot.addWidget(review)
    before = json.dumps(parent.score.to_data(), sort_keys=True)
    review.group_count.setValue(3)
    review.origin.setValue(1)
    review.set_range(1000, 4000)
    assert review.ruler.groups[:2] == [1000, 2500]
    assert json.dumps(parent.score.to_data(), sort_keys=True) == before
    review.close()
    parent.close()
    window.close()


def test_generate_through_task_then_close_recovers_candidate(qtbot, tmp_path, monkeypatch):
    from autoskyplay.transcription.engine import Engine
    from autoskyplay.transcription.task import TranscriptionTask
    window, parent = window_and_dialog(qtbot, tmp_path, monkeypatch)
    r, _, _ = prepare(parent, tmp_path)
    monkeypatch.setattr('autoskyplay.ui.refinement.select_engine', lambda *args: Engine([], tmp_path, '2.0.0', 'muscriptor'))
    region = search_context(r, Region.create(r, 3000, 6000))
    local = local_for(r, region, True)
    def run(task):
        local.save(task.work/'events.json')
        return local.metadata
    monkeypatch.setattr(TranscriptionTask, '_run_engine', run)
    review = RefinementDialog(parent)
    qtbot.addWidget(review)
    review.set_range(3000, 6000)
    review.start()
    qtbot.waitUntil(lambda: review.candidate is not None, timeout=10000)
    assert review.accept_button.isEnabled() and review.task is None
    assert review.candidate.result.analysis == r.analysis
    ident = review.candidate.ident
    review.close()
    assert CandidateStore(tmp_path).pending(parent.draft_id).ident == ident
    parent.close()
    window.close()


def test_chord_mode_is_piano_only_and_violin_returns_to_recommendation(qtbot, tmp_path, monkeypatch):
    window, parent = window_and_dialog(qtbot, tmp_path, monkeypatch)
    index = parent.generation_mode.findData('chords')
    parent.generation_mode.setCurrentIndex(index)
    parent.instrument.setCurrentIndex(parent.instrument.findData('Violin'))
    assert parent.generation_mode.currentData() == 'auto'
    assert not parent.generation_mode.model().item(index).isEnabled()
    parent.close()
    window.close()


def test_expanded_candidate_shows_actual_and_requested_ranges_after_reopen(qtbot, tmp_path, monkeypatch):
    from dataclasses import replace

    from autoskyplay.transcription.refinement import make_safe_candidate
    window, parent = window_and_dialog(qtbot, tmp_path, monkeypatch)
    r, score, params = prepare(parent, tmp_path)
    requested = search_context(r, Region.create(r, 3250, 6000))
    local = local_for(r, requested)
    local.raw_notes = [replace(n, pitch=76) if n.start_ms == 3000 else n for n in local.raw_notes]
    candidate = make_safe_candidate(parent.draft_id, r, score, params, local, requested)
    CandidateStore(tmp_path).save(candidate, local)
    review = RefinementDialog(parent)
    qtbot.addWidget(review)
    assert review.accept_button.isEnabled()
    assert review.left.value()*1000 == candidate.region.start <= 3000
    assert '原请求 3.250' in review.boundary_info.text()
    assert review.roll.requested == [3250, 6000]
    assert not review.safe_boundaries.isEnabled()
    review.discard()
    assert '原请求' not in review.boundary_info.text()
    review.safe_boundaries.setChecked(False)
    assert review.region() == Region.create(r, review.left.value()*1000, review.right.value()*1000)
    review.close()
    parent.close()
    window.close()
