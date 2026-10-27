"""Single frozen component entry; each stage runs in a fresh child process."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "build/game-src"))

from autoskyplay.transcription.worker import emit, watch_parent  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["separate", "game", "background", "features"])
    parser.add_argument("--work")
    parser.add_argument("--models")
    parser.add_argument("--model", choices=["small", "medium"], default="small")
    parser.add_argument("--device", choices=["auto", "cpu", "mps", "cuda"], default="auto")
    parser.add_argument("--parent", type=int, default=0)
    parser.add_argument("--separate-only", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--probe-devices", action="store_true")
    args = parser.parse_args()
    if args.probe_devices:
        from autoskyplay.transcription.devices import emit_probe
        emit_probe(emit, args.device)
        return
    args.game_source = None
    if args.check:
        import demucs
        import torch
        from inference.me_infer import SegmentationEstimationInferenceModel
        from muscriptor import TranscriptionModel

        from autoskyplay.transcription.engine import HYBRID_COMPONENT_VERSION

        assert callable(TranscriptionModel.load_model) and callable(SegmentationEstimationInferenceModel)
        emit("ready", engine=HYBRID_COMPONENT_VERSION, torch=torch.__version__, demucs=demucs.__version__,
             cuda_runtime=torch.version.cuda, cuda_available=torch.cuda.is_available())
        return
    if not args.work or not args.models or not args.stage:
        parser.error("--work, --models and --stage are required")
    threading.Thread(target=watch_parent, args=(args.parent,), daemon=True).start()
    from autoskyplay.transcription import worker as worker_base
    from autoskyplay.transcription.runtime_profile import RuntimeProfile

    worker_base.runtime_profile = RuntimeProfile()
    try:
        if args.stage == "separate":
            from autoskyplay.transcription.worker import run

            args.separate_only = True
        elif args.stage == "game":
            from autoskyplay.transcription.game_worker import run
        elif args.stage == "background":
            from autoskyplay.transcription.muscriptor_worker import run
        else:
            from autoskyplay.transcription.hybrid_features import run
        run(args)
    except Exception as exc:
        import traceback

        traceback.print_exc(file=sys.stderr)
        from autoskyplay.transcription.devices import error_details
        message = str(exc) if isinstance(exc, ValueError) else '混合后端阶段失败，详见阶段日志'
        if isinstance(exc, MemoryError) or 'out of memory' in str(exc).lower():
            message = '内存或显存不足，请选择 small、较短片段或 CPU 后重试；本次未保存不完整结果'
        emit(
            "error",
            **error_details(exc),
            message=message,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
