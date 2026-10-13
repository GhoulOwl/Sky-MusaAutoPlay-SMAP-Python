"""Remembered logins; tokens live in the operating system credential store."""

import json
from pathlib import Path

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "AutoSkyPlay.Cloud"
NETEASE_SERVICE = "AutoSkyPlay.Netease"


class NeteaseAuthUnavailable(ValueError):
    pass


def validate_netease_cookie(cookie: str):
    if not isinstance(cookie, str) or not any(
        part.strip().startswith("MUSIC_U=") and part.strip()[8:] for part in cookie.split(";")
    ):
        raise ValueError("已保存的网易云登录信息无效，请重新扫码登录")


def save_netease_auth(directory: Path, cookie: str):
    validate_netease_cookie(cookie)
    try:
        keyring.set_password(NETEASE_SERVICE, str(directory.resolve()), cookie)
    except Exception:
        raise NeteaseAuthUnavailable("无法保存网易云登录到系统凭据库，本次登录仍可使用") from None


def load_netease_auth(directory: Path) -> str:
    try:
        cookie = keyring.get_password(NETEASE_SERVICE, str(directory.resolve()))
    except Exception:
        raise NeteaseAuthUnavailable("无法读取系统凭据库中的网易云登录，请稍后重试") from None
    if not cookie:
        return ""
    validate_netease_cookie(cookie)
    return cookie


def clear_netease_auth(directory: Path):
    try:
        keyring.delete_password(NETEASE_SERVICE, str(directory.resolve()))
    except PasswordDeleteError:
        pass
    except Exception:
        raise NeteaseAuthUnavailable("无法清除系统凭据库中的网易云会话，已关闭自动登录") from None


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
