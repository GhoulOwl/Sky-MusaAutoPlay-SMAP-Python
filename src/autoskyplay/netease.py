"""Embedded service lifecycle and a small adapter for the unofficial NetEase API.

Only metadata and QR login are exposed. The cookie is shared by every editor and
remembered in the OS credential store, never in state.json or the environment.
"""

from __future__ import annotations

import atexit
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from .auth import (
    NeteaseAuthUnavailable,
    clear_netease_auth,
    load_netease_auth,
    save_netease_auth,
)

SERVICE_DIR = Path(__file__).resolve().parent / "assets" / "netease"


class NeteaseError(ValueError):
    pass


class NeteaseSessionExpired(NeteaseError):
    pass


def validate_port(port: int) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise NeteaseError("网易云服务端口必须是 1–65535 之间的整数")
    return port


def check_port(port: int):
    validate_port(port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise NeteaseError(f"网易云服务端口 {port} 被占用或不可用，请在设置中更换端口") from exc


class NeteaseService:
    def __init__(self, port: int, directory: Path, bundle: Path = SERVICE_DIR):
        self.port = validate_port(port)
        self.directory, self.bundle = directory, bundle
        self.token = secrets.token_urlsafe(32)
        self.process: subprocess.Popen | None = None
        self.lock = threading.RLock()
        self.closed = threading.Event()
        atexit.register(self.close)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def check_port(self, port):
        validate_port(port)
        with self.lock:
            if port == self.port and self.process and self.process.poll() is None:
                return
            check_port(port)

    def configure(self, port):
        with self.lock:
            self.check_port(port)
            if port != self.port:
                self._stop()
                self.port = port

    def ensure_running(self):
        with self.lock:
            if self.closed.is_set():
                raise NeteaseError("网易云服务已退出")
            if self.process and self.process.poll() is None:
                return
            check_port(self.port)
            node = self.bundle / "runtime" / ("node.exe" if os.name == "nt" else "node")
            if (
                not node.is_file()
                or not (self.bundle / "node_modules/@neteasecloudmusicapienhanced/api").is_dir()
            ):
                raise NeteaseError(
                    "内置网易云服务文件缺失，请重新安装应用；源码运行请先执行 scripts/prepare_netease.py"
                )
            self.directory.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update(
                AUTOSKYPLAY_NETEASE_PORT=str(self.port),
                AUTOSKYPLAY_NETEASE_TOKEN=self.token,
                AUTOSKYPLAY_PARENT_PID=str(os.getpid()),
            )
            # Node options and module paths inherited from a shell can alter the bundled service.
            env.pop("NODE_OPTIONS", None)
            env.pop("NODE_PATH", None)
            try:
                self.process = subprocess.Popen(
                    [str(node), str(self.bundle / "server.cjs")],
                    cwd=self.directory,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except OSError as exc:
                raise NeteaseError("内置网易云服务启动失败，Node 运行时无法执行，请重新安装应用") from exc
            deadline = time.monotonic() + 15
            with httpx.Client(timeout=0.5, trust_env=False) as http:
                while not self.closed.is_set() and time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        self._stop()
                        raise NeteaseError("内置网易云服务启动失败，请检查服务依赖或更换端口")
                    try:
                        reply = http.get(
                            self.base_url + "/health", headers={"Authorization": f"Bearer {self.token}"}
                        )
                        if reply.status_code == 200 and reply.json().get("service") == "AutoSkyPlay.Netease":
                            return
                    except (httpx.HTTPError, ValueError):
                        pass
                    self.closed.wait(0.1)
            self._stop()
            raise NeteaseError("内置网易云服务启动超时或已退出，请检查端口及运行依赖")

    def _stop(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.process = None

    def close(self):
        self.closed.set()
        with self.lock:
            self._stop()
        atexit.unregister(self.close)


def song_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[1-9]\d{0,18}", value):
        return value
    match = re.search(r"https?://[^\s<>\"，]+", value)
    parsed = urlparse(match[0] if match else value)
    if parsed.hostname not in ("music.163.com", "y.music.163.com"):
        raise NeteaseError("请粘贴网易云歌曲链接或有效的歌曲 ID")
    route = urlparse(parsed.fragment) if parsed.fragment.startswith("/") else parsed
    if route.path.rstrip("/") not in ("/song", "/m/song"):
        path_match = re.fullmatch(r"/(?:m/)?song/(?:id/)?([1-9]\d{0,18})/?", route.path)
        if path_match:
            return path_match[1]
        raise NeteaseError("该链接不是网易云歌曲链接，请使用歌曲分享链接")
    value = parse_qs(route.query).get("id", [""])[0]
    if not re.fullmatch(r"[1-9]\d{0,18}", value):
        raise NeteaseError("歌曲链接中缺少有效的歌曲 ID")
    return value


@dataclass(frozen=True)
class NeteaseSong:
    id: str
    name: str
    artists: str
    album: str
    cover_url: str

    @classmethod
    def parse(cls, value):
        try:
            album = value.get("al") or value.get("album") or {}
            artists = value.get("ar") or value.get("artists") or []
            result = cls(
                song_id(str(value["id"])),
                str(value["name"]),
                " / ".join(str(a["name"]) for a in artists),
                str(album.get("name", "")),
                str(album.get("picUrl", "")),
            )
            if not result.name:
                raise ValueError()
            return result
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise NeteaseError("网易云歌曲信息格式发生变化，请更新应用后重试") from exc


class NeteaseClient:
    def __init__(self, service: NeteaseService, transport=None, credential_directory: Path | None = None):
        self.service = service
        self.http = httpx.Client(timeout=20, trust_env=False, transport=transport)
        self.media = httpx.Client(timeout=15, transport=transport)
        self.lock = threading.RLock()
        self.cookie = ""
        self.user: dict = {}
        self.credential_directory = credential_directory
        self.remembered = False
        self.auth_warning = ""

    def request(self, route, **params):
        with self.lock:
            self.service.ensure_running()
            try:
                reply = self.http.post(
                    self.service.base_url + route,
                    headers={"Authorization": f"Bearer {self.service.token}"},
                    json={**params, "cookie": self.cookie, "timestamp": time.time_ns() // 1_000_000},
                )
                if reply.status_code != 200:
                    raise NeteaseError(f"网易云请求失败（HTTP {reply.status_code}），请稍后重试")
                value = reply.json()
                if not isinstance(value, dict):
                    raise ValueError()
                code = value.get("code", 200)
                if route == "/login/status" and code in (301, 401):
                    raise NeteaseSessionExpired("网易云会话已失效，请重新扫码登录")
                if code not in (200, 800, 801, 802, 803):
                    raise NeteaseError(f"网易云接口返回错误（{code}），请重新登录或稍后重试")
                return value
            except httpx.HTTPError as exc:
                raise NeteaseError("无法连接网易云服务或请求超时，请检查网络及设置中的服务端口") from exc
            except (ValueError, TypeError) as exc:
                if isinstance(exc, NeteaseError):
                    raise
                raise NeteaseError("网易云接口响应格式发生变化，请更新应用后重试") from exc

    def create_qr(self):
        value = self.request("/login/qr/key")
        data = value.get("data")
        key = data.get("unikey") if isinstance(data, dict) else None
        if not isinstance(key, str) or not key:
            raise NeteaseError("网易云未返回登录二维码，请重试")
        data = self.request("/login/qr/create", key=key, qrimg=True).get("data")
        image = data.get("qrimg") if isinstance(data, dict) else None
        if not isinstance(image, str) or not image.startswith("data:image/png;base64,"):
            raise NeteaseError("网易云登录二维码格式异常，请重试")
        return key, image.split(",", 1)[1]

    def check_qr(self, key):
        value = self.request("/login/qr/check", key=key)
        code = value.get("code")
        if code not in (800, 801, 802, 803):
            raise NeteaseError("网易云登录状态响应异常，请刷新二维码重试")
        return value

    def finish_login(self, cookie, cancelled=lambda: False, remember=True):
        with self.lock:
            if not isinstance(cookie, str) or "MUSIC_U=" not in cookie:
                raise NeteaseError("网易云未返回有效登录会话，请刷新二维码重试")
            previous = self.cookie
            self.cookie = cookie
            try:
                value = self.request("/login/status").get("data")
                if not isinstance(value, dict) or "profile" not in value:
                    raise NeteaseError("网易云登录状态格式发生变化，请更新应用后重试")
                profile = value["profile"]
                if profile is not None and not isinstance(profile, dict):
                    raise NeteaseError("网易云登录状态格式发生变化，请更新应用后重试")
                if not profile or not profile.get("userId"):
                    raise NeteaseSessionExpired("网易云会话已失效，请重新扫码登录")
                if cancelled():
                    raise NeteaseError("网易云登录已取消")
                self.auth_warning = ""
                if remember and self.credential_directory:
                    self.remembered = False
                    try:
                        save_netease_auth(self.credential_directory, cookie)
                        self.remembered = True
                    except NeteaseAuthUnavailable as exc:
                        self.auth_warning = str(exc)
                    if cancelled():
                        self.remembered = False
                        if not self.auth_warning:
                            clear_netease_auth(self.credential_directory)
                        raise NeteaseError("网易云登录已取消")
                self.user = profile
                return profile
            except Exception:
                self.cookie = previous
                raise

    def restore_login(self, cancelled=lambda: False):
        if not self.credential_directory:
            return {}
        with self.lock:
            # Keep a saved session on network/keyring failures so the user can retry.
            self.remembered = True
            try:
                cookie = load_netease_auth(self.credential_directory)
            except NeteaseAuthUnavailable:
                raise
            except ValueError:
                self.remembered = False
                clear_netease_auth(self.credential_directory)
                raise
            if not cookie:
                self.remembered = False
                return {}
            try:
                return self.finish_login(cookie, cancelled=cancelled, remember=False)
            except NeteaseSessionExpired:
                self.remembered = False
                clear_netease_auth(self.credential_directory)
                raise

    def logout(self):
        with self.lock:
            try:
                self.request("/logout")
            finally:
                self.cookie = ""
                self.user = {}
                self.http.cookies.clear()
                self.remembered = False
                if self.credential_directory:
                    clear_netease_auth(self.credential_directory)

    def resolve_identifier(self, identifier):
        match = re.search(r"https?://[^\s<>\"，）)]+", identifier)
        url = match[0] if match else identifier.strip()
        if urlparse(url).hostname != "163cn.tv":
            return song_id(identifier)
        # Current mobile share links use a short URL. Resolve only NetEase hosts,
        # without forwarding the service token or login cookie.
        try:
            for _ in range(6):
                parsed = urlparse(url)
                if parsed.hostname not in ("163cn.tv", "music.163.com", "y.music.163.com"):
                    raise NeteaseError("歌曲分享链接跳转到未知地址，请复制完整网易云歌曲链接")
                if parsed.scheme not in ("http", "https"):
                    raise NeteaseError("歌曲分享链接无效")
                if parsed.hostname != "163cn.tv":
                    return song_id(url)
                reply = self.media.get(url)
                if reply.status_code in (301, 302, 303, 307, 308) and reply.headers.get("location"):
                    url = urljoin(url, reply.headers["location"])
                else:
                    break
        except httpx.HTTPError as exc:
            raise NeteaseError("网易云歌曲分享链接解析失败，请检查网络或粘贴歌曲 ID") from exc
        raise NeteaseError("无法解析网易云歌曲分享短链接，请粘贴完整歌曲链接或歌曲 ID")

    def detail(self, identifier):
        identifier = self.resolve_identifier(identifier)
        songs = self.request("/song/detail", ids=identifier).get("songs")
        if not isinstance(songs, list):
            raise NeteaseError("网易云歌曲详情格式发生变化，请更新应用后重试")
        if not songs:
            raise NeteaseError("未找到该歌曲，请检查链接或歌曲 ID")
        song = NeteaseSong.parse(songs[0])
        if song.id != identifier:
            raise NeteaseError("网易云返回的歌曲与指定 ID 不一致，请重试")
        return song

    def search(self, keywords, offset=0):
        if not keywords.strip():
            raise NeteaseError("请输入歌曲名")
        result = self.request("/cloudsearch", keywords=keywords.strip(), type=1, limit=10, offset=offset)
        data = result.get("result")
        if not isinstance(data, dict):
            raise NeteaseError("网易云搜索格式发生变化，请更新应用后重试")
        songs = data.get("songs", [])
        if not isinstance(songs, list):
            raise NeteaseError("网易云搜索结果格式异常，请更新应用后重试")
        total = data.get("songCount", len(songs))
        if type(total) is not int or total < 0:
            raise NeteaseError("网易云搜索结果数量格式发生变化，请更新应用后重试")
        return [NeteaseSong.parse(s) for s in songs], total

    def cover(self, song: NeteaseSong) -> bytes:
        if not song.cover_url:
            raise NeteaseError("该歌曲没有专辑封面，可使用手动设置封面")
        url = song.cover_url
        if url.startswith("http://"):
            url = "https://" + url[7:]
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (
            host.endswith(".music.126.net") or host.endswith(".music.163.com")
        ):
            raise NeteaseError("网易云专辑封面地址格式发生变化，可使用手动设置封面")
        try:
            with self.media.stream("GET", url, params={"param": "512y512"}) as reply:
                reply.raise_for_status()
                data = bytearray()
                for chunk in reply.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 8 * 1024 * 1024:
                        raise NeteaseError("专辑封面超过 8 MB，可使用手动设置封面")
                return bytes(data)
        except httpx.HTTPError as exc:
            raise NeteaseError("专辑封面下载失败，请检查网络后重试，或使用手动设置封面") from exc

    def close(self):
        with self.lock:
            self.http.close()
            self.media.close()
            self.cookie = ""
            self.user = {}
