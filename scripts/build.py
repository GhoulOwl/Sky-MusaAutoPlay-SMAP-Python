"""Build on the target OS: uv run python scripts/build.py [--installer]."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from autoskyplay import __version__

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installer", action="store_true", help="Also run Inno Setup (Windows only)")
    args = parser.parse_args()
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        "AutoSkyPlay",
        "--specpath",
        str(ROOT / "build"),
        "--paths",
        str(ROOT / "src"),
        "--collect-data",
        "autoskyplay",
        "--collect-submodules",
        "keyring.backends",
        "--exclude-module",
        "PySide6.QtWebEngineCore",
        "--exclude-module",
        "PySide6.QtWebEngineWidgets",
        "--exclude-module",
        "PySide6.QtWebEngineQuick",
    ]
    if sys.platform == "win32":
        command.extend(
            [
                "--uac-admin",
                "--icon",
                str(ROOT / "src/autoskyplay/assets/app.ico"),
                "--collect-submodules",
                "pynput",
            ]
        )
    elif sys.platform == "darwin":
        command.extend(["--osx-bundle-identifier", "org.autoskyplay.desktop"])
    command.append(str(ROOT / "scripts/launcher.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    shutil.copy(ROOT / "LICENSE", ROOT / "dist/AutoSkyPlay/LICENSE")
    shutil.copy(ROOT / "THIRD_PARTY_NOTICES.md", ROOT / "dist/AutoSkyPlay/THIRD_PARTY_NOTICES.md")
    if sys.platform == "darwin":
        resources = ROOT / "dist/AutoSkyPlay.app/Contents/Resources"
        shutil.copy(ROOT / "LICENSE", resources / "LICENSE")
        shutil.copy(ROOT / "THIRD_PARTY_NOTICES.md", resources / "THIRD_PARTY_NOTICES.md")
    if args.installer:
        if sys.platform != "win32":
            parser.error("Windows 安装包必须在 Windows 上构建")
        compiler = shutil.which("iscc") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
        if not Path(compiler).exists():
            raise SystemExit("请安装 Inno Setup 6，或将 iscc 添加至 PATH")
        subprocess.run(
            [compiler, f"/DAppVersion={__version__}", str(ROOT / "installer/windows.iss")],
            cwd=ROOT,
            check=True,
        )
    elif sys.platform == "darwin":
        subprocess.run(
            [
                "hdiutil",
                "create",
                "-volname",
                "AutoSkyPlay",
                "-srcfolder",
                str(ROOT / "dist/AutoSkyPlay.app"),
                "-ov",
                "-format",
                "UDZO",
                str(ROOT / "dist/AutoSkyPlay.dmg"),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
