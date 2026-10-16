import hashlib
import io
import json
import zipfile

import pytest

from autoskyplay.transcription.engine import find_engine, install_engine, platform_tag


def component_bytes(version='3.1.0', extra=None, platform=None):
    files = {'engine': b'component executable', 'runtime.json': b'{}', 'models/models.json': b'{}'}
    files.update(extra or {})
    manifest = dict(schema=1, version=version, platform=platform or platform_tag(), entry='engine',
                    files={name: hashlib.sha256(data).hexdigest() for name, data in files.items()})
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as package:
        package.writestr('engine.json', json.dumps(manifest))
        for name, value in files.items():
            package.writestr(name, value)
    return data.getvalue()


def github_artifact(path, component, *, checksum=None, inner_name=None, extra=None):
    name = inner_name or f'AutoSkyPlay-Hybrid-3.1.0-{platform_tag()}-cu128.zip'
    checksum = checksum or hashlib.sha256(component).hexdigest()
    with zipfile.ZipFile(path, 'w') as package:
        package.writestr(name, component)
        package.writestr(name + '.sha256', f'{checksum}  {name.rsplit("/", 1)[-1]}\n')
        for key, value in (extra or {}).items():
            package.writestr(key, value)


@pytest.mark.parametrize('version,backend', [('3.1.0', 'hybrid'), ('2.1.0', 'muscriptor')])
@pytest.mark.parametrize('wrapped', [False, True])
def test_component_and_github_download_both_install_discover_and_survive_reopen(tmp_path, version, backend, wrapped):
    path = tmp_path / 'download.zip'
    data = component_bytes(version)
    if wrapped:
        github_artifact(path, data)
    else:
        path.write_bytes(data)
    directory = tmp_path / 'data'
    model = directory / 'transcription/models/retained-model'
    model.parent.mkdir(parents=True)
    model.write_bytes(b'keep existing weights')
    progress = []

    engine = install_engine(path, directory, progress=progress.append)

    assert engine.backend == backend and engine.version == version
    assert find_engine(directory, backend) == engine
    assert find_engine(directory, backend).command == engine.command
    assert model.read_bytes() == b'keep existing weights'
    assert progress[0] == 0 and progress[-1] == 100
    assert progress == sorted(progress)
    assert not list(directory.rglob('.component-import-*'))
    assert not list(directory.rglob('.staging-*'))


@pytest.mark.parametrize('damage', ['checksum', 'inner-path', 'platform', 'multiple', 'missing-checksum'])
def test_invalid_github_download_preserves_installed_component(tmp_path, damage):
    path, directory = tmp_path / 'download.zip', tmp_path / 'data'
    github_artifact(path, component_bytes())
    old = install_engine(path, directory)
    pointer = directory / 'transcription/hybrid/current.json'
    previous = pointer.read_bytes()
    payload = component_bytes(extra={'../escape': b'invalid'}) if damage == 'inner-path' else component_bytes(
        platform='wrong-platform' if damage == 'platform' else None)
    github_artifact(path, payload, checksum='0' * 64 if damage == 'checksum' else None,
                    extra={'second.zip': b'another component'} if damage == 'multiple' else None)
    if damage == 'missing-checksum':
        with zipfile.ZipFile(path, 'w') as package:
            package.writestr('component.zip', payload)

    with pytest.raises(ValueError):
        install_engine(path, directory)

    assert pointer.read_bytes() == previous
    assert find_engine(directory, 'hybrid') == old
    assert not (directory / 'transcription/escape').exists()
    assert not list(directory.rglob('.component-import-*'))
    assert not list(directory.rglob('.staging-*'))


@pytest.mark.parametrize('cancel_at', [1, 21])
def test_cancel_during_wrapper_or_component_extraction_does_not_publish(tmp_path, cancel_at):
    path, directory = tmp_path / 'download.zip', tmp_path / 'data'
    github_artifact(path, component_bytes())
    progress = []
    with pytest.raises(ValueError, match='取消'):
        install_engine(path, directory, cancelled=lambda: bool(progress and progress[-1] >= cancel_at),
                       progress=progress.append)
    assert not (directory / 'transcription/hybrid/current.json').exists()
    assert not list(directory.rglob('.component-import-*'))
    assert not list(directory.rglob('.staging-*'))


def test_artifact_cannot_escape_through_nested_archive_name(tmp_path):
    path, directory = tmp_path / 'download.zip', tmp_path / 'data'
    github_artifact(path, component_bytes(), inner_name='../component.zip')
    with pytest.raises(ValueError, match='无效路径'):
        install_engine(path, directory)
    assert not (tmp_path / 'component.zip').exists()
    assert not list(directory.rglob('current.json'))
