import pytest


@pytest.fixture(autouse=True)
def credential_store(monkeypatch, tmp_path):
    """Never read or write real project login sessions during tests."""
    directory = tmp_path / ".autoskyplay" / "credentials"
    monkeypatch.setattr("autoskyplay.auth.CREDENTIAL_DIRECTORY", directory)
    return directory
