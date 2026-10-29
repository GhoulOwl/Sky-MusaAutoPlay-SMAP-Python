"""One shared rhythm/acoustic reference from the original and the common stems."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from autoskyplay.transcription.worker import emit, watch_parent  # noqa: E402


def run(args):
    import numpy as np
    import soundfile as sf

    from autoskyplay.score import atomic_json
    from autoskyplay.transcription.signal_analysis import rhythm_harmony

    work = Path(args.work)
    settings = json.loads((work / 'analysis-request.json').read_text(encoding='utf-8')) if (work / 'analysis-request.json').exists() else {}
    multistem = settings.get('multistem', False)
    names = ('original', 'vocals', 'other', 'bass', 'drums', 'guitar', 'piano', 'lead_vocals') if multistem else (
        'original', 'vocals', 'other', 'bass', 'drums')
    waves, energy = {}, {}
    for name in names:
        y, sr = sf.read(work / f"{name}.wav", dtype="float32", always_2d=True)
        if sr != 44100 or not np.isfinite(y).all():
            raise ValueError("分轨格式或采样率不一致")
        waves[name] = y
        mono = y.mean(axis=1)
        energy[name] = [
            float(20 * np.log10(max(1e-8, np.sqrt(np.mean(mono[i : i + 4410] ** 2)))))
            for i in range(0, len(mono), 4410)
        ]
    if len({len(y) for y in waves.values()}) != 1:
        raise ValueError("分轨长度不一致")
    emit("convert", message="建立共享拍点与音轨证据")
    duration = len(waves["original"]) / 44100
    harmony = waves['other'] + waves['bass']
    vocal_name = 'lead_vocals' if multistem else 'vocals'
    rhythm, key = {}, {}
    if multistem:
        from autoskyplay.transcription.song_analysis import detect_key, detect_rhythm
        harmony += waves['guitar'] + waves['piano']
        analysis, bpm, valid, rhythm = detect_rhythm(waves['original'], waves['drums'], harmony, 44100,
                                                    args.models, detector=settings.get('detector', 'auto'))
        try:
            key = detect_key(harmony, waves['bass'], 44100, starts_song=settings.get('starts_song', False),
                             ends_song=settings.get('ends_song', False))
        except (ValueError, RuntimeError) as exc:
            key = dict(candidates=[], ambiguous=True, reason=str(exc)[:300])
    else:
        analysis, bpm, valid = rhythm_harmony(waves['original'], waves['drums'], harmony, 44100, duration)
    result = dict(
        duration_ms=duration * 1000,
        bpm=bpm,
        bpm_estimated=bool(valid),
        analysis=dict(
            beats_ms=analysis["beats_ms"].tolist(),
            beat_confidence=float(analysis["beat_confidence"]),
            chroma=analysis["chroma"].tolist(),
            harmony_energy_db=analysis["harmony_energy_db"].tolist(),
            energy_db=energy["original"],
            vocals_energy_db=energy[vocal_name],
            background_energy_db=energy["other"],
            vocals_ratio=np.clip(
                10 ** ((np.array(energy[vocal_name]) - np.array(energy["original"])) / 20), 0, 1
            ).tolist(),
            evidence_hop_ms=100,
        ),
    )
    if multistem:
        result.update(rhythm_analysis=rhythm, key_analysis=key)
        result['analysis'].update(downbeats_ms=analysis['downbeats_ms'].tolist(),
                                  detected_beats_ms=analysis['detected_beats_ms'].tolist(),
                                  stem_energy_db={n: energy[n] for n in ('bass', 'guitar', 'piano', 'other')})
    atomic_json(work / "features.json", result)
    emit("done", 100, analysis_version="hybrid-features-2", duration_ms=duration * 1000)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True)
    parser.add_argument("--models")
    parser.add_argument("--parent", type=int, default=0)
    args = parser.parse_args()
    threading.Thread(target=watch_parent, args=(args.parent,), daemon=True).start()
    try:
        run(args)
    except Exception as exc:
        import traceback

        traceback.print_exc(file=sys.stderr)
        emit("error", message=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
