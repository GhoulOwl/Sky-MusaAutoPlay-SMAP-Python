"""Remembered sessions in private project-local files, without OS keychain access."""

import hashlib
import json
import sys
from pathlib import Path

from .score import atomic_json

SERVICE = "AutoSkyPlay.Cloud"
NETEASE_SERVICE = "AutoSkyPlay.Netease"


def local_credentials_directory() -> Path:
    if not getattr(sys, "frozen", False):
        root = Path(__file__).resolve().parents[2]
    else:
        executable = Path(sys.executable).resolve()
        root = next(
            (
                parent for parent in executable.parents
                if (parent / "pyproject.toml").is_file() and (parent / "src" / "autoskyplay").is_dir()
            ),
            None,
        )
        if root is None:
            bundle = next((parent for parent in executable.parents if parent.suffix == ".app"), None)
            root = bundle.parent if bundle else executable.parent
    return root / ".autoskyplay" / "credentials"


CREDENTIAL_DIRECTORY = local_credentials_directory()


class NeteaseAuthUnavailable(ValueError):
    pass


def credential_path(service: str, account: str) -> Path:
    digest = hashlib.sha256(account.encode("utf-8")).hexdigest()
    prefix = "netease" if service == NETEASE_SERVICE else "cloud"
    return CREDENTIAL_DIRECTORY / f"{prefix}-{digest}.json"


def _write_credential(service: str, account: str, value):
    CREDENTIAL_DIRECTORY.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    CREDENTIAL_DIRECTORY.mkdir(mode=0o700, exist_ok=True)
    # atomic_json uses a private mkstemp file (0600), then replaces the target.
    atomic_json(credential_path(service, account), {
        "version": 1, "service": service, "account": account, "value": value,
    })


def _read_credential(service: str, account: str):
    path = credential_path(service, account)
    try:
        with path.open(encoding="utf-8") as source:
            record = json.load(source)
    except FileNotFoundError:
        return None
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("本地登录凭据文件已损坏，请重新登录") from None
    if (
        not isinstance(record, dict) or record.get("version") != 1
        or record.get("service") != service or record.get("account") != account
        or "value" not in record
    ):
        raise ValueError("本地登录凭据文件无效，请重新登录")
    return record["value"]


def _delete_credential(service: str, account: str):
    credential_path(service, account).unlink(missing_ok=True)


def validate_netease_cookie(cookie: str):
    if not isinstance(cookie, str) or not any(
        part.strip().startswith("MUSIC_U=") and part.strip()[8:] for part in cookie.split(";")
    ):
        raise ValueError("已保存的网易云登录信息无效，请重新扫码登录")


def save_netease_auth(directory: Path, cookie: str):
    validate_netease_cookie(cookie)
    try:
        _write_credential(NETEASE_SERVICE, str(directory.resolve()), cookie)
    except Exception:
        raise NeteaseAuthUnavailable("无法保存网易云登录到本地凭据文件，本次登录仍可使用") from None


def load_netease_auth(directory: Path) -> str:
    try:
        cookie = _read_credential(NETEASE_SERVICE, str(directory.resolve()))
    except OSError:
        raise NeteaseAuthUnavailable("无法读取本地凭据文件中的网易云登录，请稍后重试") from None
    if cookie is None:
        return ""
    validate_netease_cookie(cookie)
    return cookie


def clear_netease_auth(directory: Path):
    try:
        _delete_credential(NETEASE_SERVICE, str(directory.resolve()))
    except Exception:
        raise NeteaseAuthUnavailable("无法清除本地凭据文件中的网易云会话，已关闭自动登录") from None


def save_auth(base_url: str, user: dict):
    if not isinstance(user, dict) or not isinstance(user.get("mid"), str) or not user["mid"]:
        raise ValueError("登录令牌无效，请重新登录")
    # Persist only the token and profile fields used by the app, never account passwords.
    value = {key: user[key] for key in ("id", "username", "mid", "avatar") if key in user}
    try:
        _write_credential(SERVICE, base_url, value)
    except Exception:
        raise ValueError("无法保存缪斯树屋登录到本地凭据文件，本次登录仍可使用") from None


def load_auth(base_url: str) -> dict:
    try:
        value = _read_credential(SERVICE, base_url)
    except OSError:
        raise ValueError("无法读取本地凭据文件中的缪斯树屋登录，请稍后重试") from None
    if value is None:
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("mid"), str) or not value["mid"]:
        raise ValueError("已保存的登录信息无效，请重新登录")
    return value


def clear_auth(base_url: str):
    try:
        _delete_credential(SERVICE, base_url)
    except Exception:
        raise ValueError("无法清除本地凭据文件中的缪斯树屋登录") from None
