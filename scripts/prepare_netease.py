"""Install locked service dependencies and a verified Node runtime for this OS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "src/autoskyplay/assets/netease"
NODE_VERSION = "22.22.0"


def prepare():
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise SystemExit("准备网易云服务需要构建机安装 Node.js 22+（包含 npm）；发行包用户无需安装")
    # No dependency lifecycle scripts run on the build machine.
    subprocess.run(
        [npm, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=BUNDLE, check=True
    )
    target = {"Windows": "win", "Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    arch = {"AMD64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine())
    if not target or not arch:
        raise SystemExit("网易云内置 Node 运行时目前支持 Windows/macOS/Linux 的 x64 与 arm64")
    prefix = f"node-v{NODE_VERSION}-{target}-{arch}"
    archive = prefix + (".zip" if target == "win" else ".tar.gz")
    base = f"https://nodejs.org/dist/v{NODE_VERSION}/"
    runtime = BUNDLE / "runtime"
    executable = runtime / ("node.exe" if target == "win" else "node")
    marker = runtime / "runtime.json"
    expected = {"version": NODE_VERSION, "target": target, "arch": arch}
    if executable.exists() and marker.exists() and json.loads(marker.read_text()) == expected:
        return
    with urllib.request.urlopen(base + "SHASUMS256.txt", timeout=60) as response:
        checksums = dict(line.split()[::-1] for line in response.read().decode().splitlines())
    expected_hash = checksums[archive]
    with tempfile.TemporaryDirectory(prefix="autoskyplay-node-") as temp:
        download = Path(temp) / archive
        with urllib.request.urlopen(base + archive, timeout=60) as response, download.open("wb") as output:
            shutil.copyfileobj(response, output)
        with download.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected_hash:
                raise SystemExit("Node 下载校验失败，请重试")
        staged = Path(temp) / "runtime"
        staged.mkdir()
        members = (
            {"node.exe": "node.exe", "LICENSE": "LICENSE"}
            if target == "win"
            else {"bin/node": "node", "LICENSE": "LICENSE"}
        )
        # Extract only exact known files, never paths supplied by the archive.
        if target == "win":
            with zipfile.ZipFile(download) as package:
                for member, name in members.items():
                    (staged / name).write_bytes(package.read(prefix + "/" + member))
        else:
            with tarfile.open(download) as package:
                for member, name in members.items():
                    with package.extractfile(prefix + "/" + member) as stream:
                        (staged / name).write_bytes(stream.read())
            (staged / "node").chmod(0o755)
        (staged / "runtime.json").write_text(json.dumps(expected), encoding="utf-8")
        runtime.mkdir(exist_ok=True)
        for file in staged.iterdir():
            shutil.copy2(file, runtime / file.name)


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    prepare()
