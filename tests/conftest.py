import pytest
from keyring.errors import PasswordDeleteError


@pytest.fixture
def credential_store(monkeypatch):
    """Use an isolated credential backend; never touch the user's real keychain."""
    values = {}
    monkeypatch.setattr(
        "autoskyplay.auth.keyring.set_password",
        lambda service, account, value: values.update({(service, account): value}),
    )
    monkeypatch.setattr(
        "autoskyplay.auth.keyring.get_password", lambda service, account: values.get((service, account))
    )

    def delete(service, account):
        try:
            del values[service, account]
        except KeyError:
            raise PasswordDeleteError() from None

    monkeypatch.setattr("autoskyplay.auth.keyring.delete_password", delete)
    return values
