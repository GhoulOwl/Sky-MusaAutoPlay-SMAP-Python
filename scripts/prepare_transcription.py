"""Prepare the isolated CPU engine; --package creates an offline component ZIP."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "build/transcription-models"
ENVIRONMENT = ROOT / ".transcription-venv"
PYTHON = ENVIRONMENT / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download_models():
    import httpx

    manifest_path = ROOT / "engine/models.json"
    manifest = json.loads(manifest_path.read_text())
    MODELS.mkdir(parents=True, exist_ok=True)
    for name, checksum in manifest["files"].items():
        target = MODELS / name
        if target.exists() and digest(target) == checksum:
            continue
        temporary = target.with_suffix(target.suffix + ".part")
        url = f"https://huggingface.co/{manifest['repository']}/resolve/{manifest['revision']}/{name}"
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        output.write(chunk)
            if digest(temporary) != checksum:
                raise ValueError("模型文件校验失败：" + name)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    shutil.copyfile(manifest_path, MODELS / "models.json")


def collect_licenses(destination):
    import imageio_ffmpeg

    destination.mkdir(exist_ok=True)
    with (destination / "FFmpeg-license.txt").open("wb") as output:
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-L"], stdout=output,
                       stderr=subprocess.STDOUT, check=True)
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        for file in distribution.files or []:
            if any(token in file.name.upper() for token in ("LICENSE", "COPYING", "NOTICE")):
                source = distribution.locate_file(file)
                if source.is_file():
                    target = destination / name / Path(str(file))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)


def package_engine():
    from autoskyplay.transcription.engine import ENGINE_VERSION, platform_tag

    worker = ROOT / "src/autoskyplay/transcription/worker.py"
    subprocess.run([
        str(PYTHON), "-m", "PyInstaller", "--noconfirm", "--clean", "--console",
        "--paths", str(worker.parent), "--paths", str(ROOT / 'src'), "--hidden-import", "signal_analysis",
        "--name", "AutoSkyPlay-Transcriber", "--distpath", str(ROOT / "build/transcription-dist"),
        "--workpath", str(ROOT / "build/transcription-pyinstaller"), "--specpath", str(ROOT / "build"),
        "--collect-data", "torchcrepe", "--collect-data", "imageio_ffmpeg",
        "--collect-submodules", "librosa", "--hidden-import", "demucs.htdemucs",
        "--hidden-import", "demucs.hdemucs", "--hidden-import", "demucs.demucs",
        "--copy-metadata", "demucs", "--copy-metadata", "librosa",
        "--exclude-module", "matplotlib", "--exclude-module", "IPython", str(worker),
    ], cwd=ROOT, check=True)
    bundle = ROOT / "build/transcription-dist/AutoSkyPlay-Transcriber"
    shutil.copytree(MODELS, bundle / "models", dirs_exist_ok=True)
    shutil.copyfile(ROOT / "LICENSE", bundle / "LICENSE")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", bundle / "THIRD_PARTY_NOTICES.md")
    subprocess.run([str(PYTHON), str(Path(__file__).resolve()), "--licenses", str(bundle / "licenses")],
                   cwd=ROOT, check=True)
    entry = "AutoSkyPlay-Transcriber" + (".exe" if os.name == "nt" else "")
    subprocess.run([str(bundle / entry), "--check"], check=True)
    files = {file.relative_to(bundle).as_posix(): digest(file)
             for file in sorted(bundle.rglob("*")) if file.is_file() and file.name != "engine.json"}
    manifest = dict(schema=1, version=ENGINE_VERSION, platform=platform_tag(), entry=entry, files=files)
    (bundle / "engine.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    target = ROOT / "dist" / f"AutoSkyPlay-Transcription-{ENGINE_VERSION}-{platform_tag()}.zip"
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(bundle.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(bundle).as_posix())
    target.with_suffix(".zip.sha256").write_text(digest(target) + "  " + target.name + "\n")
    print("Offline component:", target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", action="store_true")
    parser.add_argument("--licenses", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.licenses:
        collect_licenses(args.licenses)
        return
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("准备本地扒谱引擎需要 uv；发行包用户无需安装")
    if not PYTHON.exists():
        subprocess.run([uv, "venv", "--python", "3.12", str(ENVIRONMENT)], check=True)
    subprocess.run([uv, "pip", "sync", "--python", str(PYTHON), "--require-hashes", "--quiet",
                    "--extra-index-url", "https://download.pytorch.org/whl/cpu",
                    "--index-strategy", "unsafe-best-match", str(ROOT / "engine/requirements.lock")], check=True)
    download_models()
    subprocess.run([str(PYTHON), str(ROOT / "src/autoskyplay/transcription/worker.py"), "--check"], check=True)
    if args.package:
        package_engine()


if __name__ == "__main__":
    main()
