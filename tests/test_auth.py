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
