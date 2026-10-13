import json
import os
import sys
from pathlib import Path

import pytest

from autoskyplay import auth
from autoskyplay.auth import clear_auth, load_auth, save_auth


def test_remembered_token_persists_separately_from_state(credential_store):
    user = {"id": 1, "username": "tester", "mid": "test-token", "avatar": "test.png"}
    save_auth("https://example.org", {**user, "password": "never-save"})
    assert load_auth("https://example.org") == user
    path = auth.credential_path(auth.SERVICE, "https://example.org")
    assert path.parent == credential_store
    assert "never-save" not in path.read_text()
    assert load_auth("https://other.example.org") == {}
    clear_auth("https://example.org")
    assert load_auth("https://example.org") == {} and not path.exists()
    clear_auth("https://example.org")


def test_netease_credentials_persist_and_are_scoped_to_data_directory(tmp_path, credential_store):
    first, second = tmp_path / "first", tmp_path / "second"
    assert auth.load_netease_auth(first) == ""
    auth.save_netease_auth(first, "MUSIC_U=session; __csrf=token")
    auth.save_netease_auth(second, "MUSIC_U=other")
    assert auth.load_netease_auth(first) == "MUSIC_U=session; __csrf=token"
    assert auth.load_netease_auth(second) == "MUSIC_U=other"
    assert len(list(credential_store.glob("netease-*.json"))) == 2
    assert not first.exists() and not second.exists()
    auth.clear_netease_auth(first)
    assert auth.load_netease_auth(first) == ""
    assert auth.load_netease_auth(second) == "MUSIC_U=other"
    auth.clear_netease_auth(first)


def test_netease_invalid_saved_cookie_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="无效"):
        auth.save_netease_auth(tmp_path, "MUSIC_U=;")
    auth._write_credential(auth.NETEASE_SERVICE, str(tmp_path.resolve()), "broken")
    with pytest.raises(ValueError, match="无效"):
        auth.load_netease_auth(tmp_path)


def test_corrupt_file_error_does_not_expose_credential(tmp_path):
    auth.save_netease_auth(tmp_path, "MUSIC_U=secret")
    path = auth.credential_path(auth.NETEASE_SERVICE, str(tmp_path.resolve()))
    path.write_text("MUSIC_U=secret", encoding="utf-8")
    with pytest.raises(ValueError, match="损坏") as error:
        auth.load_netease_auth(tmp_path)
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__


def test_record_identity_and_token_are_validated():
    url = "https://example.org"
    auth._write_credential(auth.SERVICE, url, {"mid": 123})
    with pytest.raises(ValueError, match="无效"):
        load_auth(url)
    path = auth.credential_path(auth.SERVICE, url)
    record = json.loads(path.read_text())
    record["account"] = "https://other.example.org"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="无效"):
        load_auth(url)


def test_write_failure_preserves_previous_session_without_exposing_token(tmp_path, monkeypatch):
    auth.save_netease_auth(tmp_path, "MUSIC_U=old")

    def fail(*args):
        raise OSError("MUSIC_U=secret was not stored")

    monkeypatch.setattr("autoskyplay.auth.atomic_json", fail)
    with pytest.raises(auth.NeteaseAuthUnavailable, match="无法保存") as error:
        auth.save_netease_auth(tmp_path, "MUSIC_U=secret")
    assert "secret" not in str(error.value) and error.value.__suppress_context__
    assert auth.load_netease_auth(tmp_path) == "MUSIC_U=old"


def test_cloud_write_failure_does_not_expose_token(monkeypatch):
    def fail(*args):
        raise OSError("secret-token")

    monkeypatch.setattr("autoskyplay.auth._write_credential", fail)
    with pytest.raises(ValueError, match="无法保存") as error:
        save_auth("https://example.org", {"mid": "secret-token"})
    assert "secret-token" not in str(error.value) and error.value.__suppress_context__


def test_credential_files_have_private_permissions(credential_store):
    if os.name == "nt":
        pytest.skip("POSIX permission bits do not apply on Windows")
    save_auth("https://example.org", {"mid": "token"})
    path = auth.credential_path(auth.SERVICE, "https://example.org")
    assert credential_store.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    save_auth("https://example.org", {"mid": "replacement"})
    assert path.stat().st_mode & 0o777 == 0o600


def test_source_credentials_are_relative_to_project_not_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.chdir(tmp_path)
    assert auth.local_credentials_directory() == (
        Path(auth.__file__).resolve().parents[2] / ".autoskyplay" / "credentials"
    )


def test_packaged_app_inside_project_uses_same_project_directory(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "src" / "autoskyplay").mkdir(parents=True)
    executable = tmp_path / "dist" / "workbench" / "AutoSkyPlay.app" / "Contents" / "MacOS" / "AutoSkyPlay"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert auth.local_credentials_directory() == tmp_path / ".autoskyplay" / "credentials"


@pytest.mark.parametrize("relative, expected", [
    ("AutoSkyPlay.app/Contents/MacOS/AutoSkyPlay", ".autoskyplay/credentials"),
    ("AutoSkyPlay/AutoSkyPlay.exe", "AutoSkyPlay/.autoskyplay/credentials"),
])
def test_standalone_package_stores_beside_app(tmp_path, monkeypatch, relative, expected):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / relative))
    assert auth.local_credentials_directory() == tmp_path / expected
