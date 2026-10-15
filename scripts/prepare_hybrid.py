"""Build the offline hybrid component with pinned GAME/MuScriptor source revisions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from autoskyplay.transcription.engine import platform_tag
from autoskyplay.transcription.game_models import GAME_REVISION
from autoskyplay.transcription.models import RUNTIME_REVISION

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / "build/hybrid-venv"
PYTHON = ENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(*args):
    subprocess.run([str(x) for x in args], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--package", action="store_true")
    args = parser.parse_args()
    if not args.skip_install:
        if not PYTHON.exists():
            run("uv", "venv", "--python", "3.12", ENV)
        for name, url, revision in (
            ("game", "https://github.com/openvpi/GAME.git", GAME_REVISION),
            ("muscriptor", "https://github.com/muscriptor/muscriptor.git", RUNTIME_REVISION),
        ):
            source = ROOT / f"build/{name}-src"
            if not source.exists():
                run("git", "clone", url, source)
            run("git", "-C", source, "checkout", "--detach", revision)
        run("uv", "pip", "install", "--python", PYTHON, "-r", ROOT / "engine/hybrid-requirements.lock")
        run("uv", "pip", "install", "--python", PYTHON, "--no-deps", ROOT / "build/muscriptor-src")
    for name, revision in (("game", GAME_REVISION), ("muscriptor", RUNTIME_REVISION)):
        source = ROOT / f"build/{name}-src"
        actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if actual != revision:
            raise ValueError(f"{name} 源码修订与固定版本不符")
        run("git", "-C", source, "diff", "--exit-code", "HEAD")
    worker = ROOT / "src/autoskyplay/transcription/hybrid_worker.py"
    runtime = dict(
        version="3.0.0",
        game_revision=GAME_REVISION,
        muscriptor_revision=RUNTIME_REVISION,
        process_policy="sequential; separate/game/background/features are separate processes",
    )
    (ENV / "runtime.json").write_text(json.dumps(runtime, indent=2))
    run(PYTHON, worker, "--check")
    if not args.package:
        return
    # Fixed Demucs files are shared with the old preparer. This downloads only
    # missing pinned separation files, never GAME/MuScriptor weights at inference.
    from prepare_transcription import download_models

    download_models()
    run(
        PYTHON,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--console",
        "--name",
        "AutoSkyPlay-Hybrid",
        "--paths",
        ROOT / "src",
        "--paths",
        ROOT / "build/game-src",
        "--distpath",
        ROOT / "build/hybrid-dist",
        "--workpath",
        ROOT / "build/hybrid-pyinstaller",
        "--specpath",
        ROOT / "build",
        "--collect-data",
        "imageio_ffmpeg",
        "--collect-submodules",
        "librosa",
        "--copy-metadata",
        "librosa",
        "--copy-metadata",
        "demucs",
        "--collect-submodules",
        "scipy._external",
        "--collect-submodules",
        "modules.backbones",
        "--hidden-import",
        "modules.backbones.EBF",
        "--hidden-import",
        "modules.backbones.ebf_with_joint_attention",
        "--exclude-module",
        "PySide6",
        "--exclude-module",
        "IPython",
        "--exclude-module",
        "matplotlib",
        "--exclude-module",
        "lightning",
        "--exclude-module",
        "torchcrepe",
        worker,
    )
    bundle = ROOT / "build/hybrid-dist/AutoSkyPlay-Hybrid"
    shutil.copy2(ENV / "runtime.json", bundle / "runtime.json")
    shutil.copytree(ROOT / "build/transcription-models", bundle / "models", dirs_exist_ok=True)
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", bundle / "THIRD_PARTY_NOTICES.md")
    for name in ("game", "muscriptor"):
        shutil.copy2(ROOT / f"build/{name}-src/LICENSE", bundle / f"{name}-LICENSE")
    run(PYTHON, ROOT / "scripts/prepare_transcription.py", "--licenses", bundle / "licenses")
    entry = "AutoSkyPlay-Hybrid" + (".exe" if os.name == "nt" else "")
    run(bundle / entry, "--check")
    files = {}
    for file in sorted(bundle.rglob("*")):
        if file.is_file() and file.name != "engine.json":
            with file.open("rb") as stream:
                files[file.relative_to(bundle).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    (bundle / "engine.json").write_text(
        json.dumps(dict(schema=1, version="3.0.0", platform=platform_tag(), entry=entry, files=files))
    )
    target = ROOT / "dist" / f"AutoSkyPlay-Hybrid-3.0.0-{platform_tag()}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(bundle.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(bundle))
    with target.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    target.with_suffix(".zip.sha256").write_text(f"{checksum}  {target.name}\n")
    print(target, flush=True)


if __name__ == "__main__":
    main()
