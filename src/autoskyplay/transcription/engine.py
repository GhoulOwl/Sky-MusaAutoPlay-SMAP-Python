"""Install verified, platform-specific offline engine packages atomically."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..score import atomic_json

ENGINE_VERSION = "1.1.0"
MUSCRIPTOR_VERSION = '2.0.0'
HYBRID_COMPONENT_VERSION = '3.0.0'
SUPPORTED_VERSIONS = {"1.0.0", ENGINE_VERSION, MUSCRIPTOR_VERSION, HYBRID_COMPONENT_VERSION}


def platform_tag():
    system = {"darwin": "macos", "win32": "windows", "linux": "linux"}.get(sys.platform, sys.platform)
    arch = {"AMD64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine(), platform.machine())
    return f"{system}-{arch}"


def relative_file(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or "\\" in name or ":" in name
            or any(p in ("", ".", "..") for p in name.split("/"))):
        raise ValueError("扒谱组件包包含无效路径")
    return path


@dataclass(frozen=True)
class Engine:
    command: list[str]
    models: Path
    version: str = ENGINE_VERSION
    backend: str = 'legacy'
    model: str = 'small'
    device: str = 'auto'
    config: dict = field(default_factory=dict)


def select_hybrid_engine(directory, model='small', device='auto', background_mix='with_drums'):
    """Choose the separate component without changing the original-mix backend."""
    from dataclasses import replace

    from .game_models import GameModelStore
    from .models import ModelStore
    if model not in ('small', 'medium') or background_mix not in ('with_drums', 'without_drums'):
        raise ValueError('混合后端配置无效')
    if device not in ('auto', 'cpu', 'mps', 'cuda'):
        raise ValueError('设备设置无效')
    if not ModelStore(directory).installed(model) or not GameModelStore(directory).installed('medium'):
        raise ValueError('请先安装 MuScriptor 和 GAME medium 模型')
    installed = find_engine(directory, 'hybrid')
    if installed:
        return replace(installed, model=model, device=device,
                       config=dict(installed.config, background_mix=background_mix, game_device='cpu'))
    if getattr(sys, 'frozen', False):
        raise ValueError('请先导入混合后端 3.0.0 运行组件')
    root = Path(__file__).resolve().parents[3]
    suffix = 'Scripts/python.exe' if os.name == 'nt' else 'bin/python'
    commands = {
        'separate': [str(root/'.transcription-venv'/suffix), str(Path(__file__).with_name('worker.py'))],
        'game': [str(root/'build/game-venv'/suffix), str(Path(__file__).with_name('game_worker.py'))],
        'background': [str(root/'build/muscriptor-venv'/suffix), str(Path(__file__).with_name('muscriptor_worker.py'))],
    }
    if any(not Path(c[0]).is_file() for c in commands.values()):
        raise ValueError('混合运行环境尚未安装')
    return Engine([], (directory/'transcription/models').resolve(), 'hybrid-1', 'hybrid', model, device,
                  dict(commands=commands, separation_models=str(root/'build/transcription-models'),
                       background_mix=background_mix, game_device='cpu'))


def find_engine(directory: Path, backend='legacy') -> Engine | None:
    base = (directory / "transcription").resolve()
    if backend in ('muscriptor', 'hybrid'):
        base /= backend
    pointer = base / "current.json"
    if pointer.exists():
        try:
            identity = json.loads(pointer.read_text())["component"]
            if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
                raise ValueError()
            root = base / "components" / identity
            metadata = json.loads((root / "engine.json").read_text())
            if metadata["platform"] != platform_tag() or metadata["version"] not in SUPPORTED_VERSIONS:
                raise ValueError()
            executable = root.joinpath(*relative_file(metadata["entry"]).parts)
            with executable.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != metadata["files"][metadata["entry"]]:
                    raise ValueError()
            actual = {MUSCRIPTOR_VERSION: 'muscriptor', HYBRID_COMPONENT_VERSION: 'hybrid'}.get(metadata['version'], 'legacy')
            if actual != backend:
                raise ValueError()
            if backend == 'hybrid':
                commands = {name: [str(executable), '--stage', name]
                            for name in ('separate', 'game', 'background', 'features')}
                return Engine([str(executable)], (directory/'transcription/models').resolve(),
                              metadata['version'], backend, config=dict(commands=commands,
                              runtime_identity=metadata['files'][metadata['entry']],
                              separation_models=str(root/'models'), background_mix='with_drums', game_device='cpu'))
            return Engine([str(executable)], (directory / 'transcription/models').resolve()
                          if backend == 'muscriptor' else root / 'models', metadata['version'], backend,
                          config=dict(runtime_identity=metadata['files'][metadata['entry']]))
        except (OSError, KeyError, TypeError, ValueError):
            raise ValueError("已安装的扒谱组件损坏或版本不匹配，请重新导入组件包") from None
    if not getattr(sys, "frozen", False):
        root = Path(__file__).resolve().parents[3]
        if backend == 'hybrid':
            return None  # select_hybrid_engine validates the three development environments.
        if backend == 'muscriptor':
            python = root / 'build/muscriptor-venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            if python.is_file() and (root / 'build/muscriptor-venv/runtime.json').is_file():
                return Engine([str(python), str(Path(__file__).with_name('muscriptor_worker.py'))],
                              (directory / 'transcription/models').resolve(), MUSCRIPTOR_VERSION, backend,
                              config=dict(runtime_identity=hashlib.sha256(
                                  Path(__file__).with_name('muscriptor_worker.py').read_bytes()).hexdigest()))
            return None
        python = root / ".transcription-venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        models = root / "build/transcription-models"
        if python.is_file() and (models / "models.json").is_file():
            return Engine([str(python), str(Path(__file__).with_name("worker.py"))], models)
    return None


def install_engine(archive: Path, directory: Path, cancelled=lambda: False, progress=lambda value: None):
    base = (directory / "transcription").resolve()
    identity = uuid.uuid4().hex
    stage = base / "components" / (".staging-" + identity)
    stage.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as package:
            members = [i for i in package.infolist() if not i.is_dir()]
            if len(members) > 50000 or sum(i.file_size for i in members) > 6 * 1024 ** 3:
                raise ValueError("扒谱组件包过大")
            names = [i.filename for i in members]
            if len(names) != len(set(names)) or "engine.json" not in names:
                raise ValueError("扒谱组件清单缺失或有重复文件")
            if package.getinfo("engine.json").file_size > 8 * 1024 * 1024:
                raise ValueError("扒谱组件清单过大")
            manifest = json.loads(package.read("engine.json"))
            if (manifest.get("schema") != 1 or manifest.get("version") not in SUPPORTED_VERSIONS
                    or manifest.get("platform") != platform_tag()):
                raise ValueError("组件版本或平台不匹配，请选择 " + platform_tag() + " 组件包")
            if manifest['version'] in (MUSCRIPTOR_VERSION, HYBRID_COMPONENT_VERSION):
                # A separate pointer leaves the legacy regression engine intact.
                shutil.rmtree(stage)
                base /= 'hybrid' if manifest['version'] == HYBRID_COMPONENT_VERSION else 'muscriptor'
                stage = base / 'components' / ('.staging-' + identity)
                stage.mkdir(parents=True)
            files = manifest.get("files")
            if not isinstance(files, dict) or set(names) != {*files, "engine.json"}:
                raise ValueError("扒谱组件文件与清单不一致")
            entry = manifest.get("entry", "")
            relative_file(entry)
            required = 'runtime.json' if manifest['version'] in (MUSCRIPTOR_VERSION, HYBRID_COMPONENT_VERSION) else 'models/models.json'
            if entry not in files or required not in files:
                raise ValueError("扒谱组件缺少引擎或模型")
            if manifest['version'] == HYBRID_COMPONENT_VERSION and 'models/models.json' not in files:
                raise ValueError('混合组件缺少固定分轨模型清单')
            for index, member in enumerate(members):
                if cancelled():
                    raise ValueError("组件安装已取消")
                path = stage.joinpath(*relative_file(member.filename).parts)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                    raise ValueError("组件包不能包含链接或特殊文件")
                path.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with package.open(member) as source, path.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        if cancelled():
                            raise ValueError("组件安装已取消")
                        digest.update(chunk)
                        output.write(chunk)
                if member.filename != "engine.json" and digest.hexdigest() != files[member.filename]:
                    raise ValueError("扒谱组件文件校验失败，请重新下载组件包")
                if mode & 0o111 or member.filename == entry:
                    path.chmod(0o755)
                progress(int((index + 1) * 100 / len(members)))
        destination = stage.with_name(identity)
        stage.rename(destination)
        atomic_json(base / "current.json", {"component": identity})
        return find_engine(directory, {MUSCRIPTOR_VERSION:'muscriptor', HYBRID_COMPONENT_VERSION:'hybrid'}.get(
                           manifest['version'], 'legacy'))
    except (zipfile.BadZipFile, KeyError, TypeError, json.JSONDecodeError):
        raise ValueError("扒谱组件包格式无效，请选择构建生成的组件 ZIP") from None
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def clear_components(directory):
    shutil.rmtree(directory / "transcription" / "components", ignore_errors=True)
    (directory / "transcription" / "current.json").unlink(missing_ok=True)
    shutil.rmtree(directory / 'transcription/muscriptor/components', ignore_errors=True)
    (directory / 'transcription/muscriptor/current.json').unlink(missing_ok=True)
    shutil.rmtree(directory / 'transcription/hybrid/components', ignore_errors=True)
    (directory / 'transcription/hybrid/current.json').unlink(missing_ok=True)


def select_engine(directory, model='small', device='auto'):
    from dataclasses import replace

    from .models import MODELS, ModelStore
    if model not in MODELS or device not in ('auto', 'cpu', 'mps', 'cuda'):
        raise ValueError('模型或设备设置无效')
    engine = find_engine(directory, 'muscriptor')
    if not engine:
        raise ValueError('请先导入 MuScriptor 2.0.0 运行组件')
    if not ModelStore(directory).installed(model):
        raise ValueError(f'请在设置 → 扒谱模型管理中下载 MuScriptor {model}')
    return replace(engine, model=model, device=device)
