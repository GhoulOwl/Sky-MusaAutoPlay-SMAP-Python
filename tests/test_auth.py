from autoskyplay.auth import clear_auth, load_auth, save_auth


def test_remembered_token_is_separate_from_application_state(monkeypatch):
    stored = {}
    monkeypatch.setattr(
        "autoskyplay.auth.keyring.set_password", lambda service, user, value: stored.update({user: value})
    )
    monkeypatch.setattr("autoskyplay.auth.keyring.get_password", lambda service, user: stored.get(user))
    monkeypatch.setattr("autoskyplay.auth.keyring.delete_password", lambda service, user: stored.pop(user))
    user = {"id": 1, "username": "tester", "mid": "test-token"}
    save_auth("https://example.org", user)
    assert load_auth("https://example.org") == user
    clear_auth("https://example.org")
    assert load_auth("https://example.org") == {}


def test_netease_credentials_persist_and_are_scoped_to_data_directory(tmp_path, credential_store):
    from autoskyplay.auth import clear_netease_auth, load_netease_auth, save_netease_auth

    first, second = tmp_path / "first", tmp_path / "second"
    assert load_netease_auth(first) == ""
    save_netease_auth(first, "MUSIC_U=session; __csrf=token")
    assert load_netease_auth(first) == "MUSIC_U=session; __csrf=token"
    assert load_netease_auth(second) == ""
    assert not list(tmp_path.iterdir())
    clear_netease_auth(first)
    assert load_netease_auth(first) == ""
    clear_netease_auth(first)


def test_netease_invalid_saved_cookie_is_rejected(tmp_path, credential_store):
    import pytest

    from autoskyplay.auth import NETEASE_SERVICE, load_netease_auth, save_netease_auth

    with pytest.raises(ValueError, match="无效"):
        save_netease_auth(tmp_path, "MUSIC_U=;")
    credential_store[NETEASE_SERVICE, str(tmp_path.resolve())] = "broken"
    with pytest.raises(ValueError, match="无效"):
        load_netease_auth(tmp_path)


def test_netease_keyring_errors_do_not_expose_cookie(tmp_path, monkeypatch):
    import pytest

    from autoskyplay.auth import NeteaseAuthUnavailable, save_netease_auth

    def fail(*args):
        raise RuntimeError("MUSIC_U=secret was not stored")

    monkeypatch.setattr("autoskyplay.auth.keyring.set_password", fail)
    with pytest.raises(NeteaseAuthUnavailable, match="无法保存") as error:
        save_netease_auth(tmp_path, "MUSIC_U=secret")
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__
