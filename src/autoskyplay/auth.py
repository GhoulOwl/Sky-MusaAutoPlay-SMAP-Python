"""Optional remembered login; tokens live in the operating system credential store."""

import json

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "AutoSkyPlay.Cloud"


def save_auth(base_url: str, user: dict):
    # Never persist the password, and keep the server token out of state.json.
    keyring.set_password(SERVICE, base_url, json.dumps(user))


def load_auth(base_url: str) -> dict:
    content = keyring.get_password(SERVICE, base_url)
    if not content:
        return {}
    value = json.loads(content)
    if not isinstance(value, dict) or not value.get("mid"):
        raise ValueError("已保存的登录信息无效，请重新登录")
    return value


def clear_auth(base_url: str):
    try:
        keyring.delete_password(SERVICE, base_url)
    except PasswordDeleteError:
        pass
