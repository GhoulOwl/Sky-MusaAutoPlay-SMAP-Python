import json
import os
import shutil
import socket
import threading
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from autoskyplay.netease import (
    SERVICE_DIR,
    NeteaseClient,
    NeteaseError,
    NeteaseNetworkError,
    NeteaseService,
    NeteaseSessionExpired,
    NeteaseSong,
    check_port,
    song_id,
    validate_port,
)
from autoskyplay.storage import Store

SONG = {
    "id": 186016,
    "name": "晴天",
    "ar": [{"name": "周杰伦"}, {"name": "合唱"}],
    "al": {"name": "叶惠美", "picUrl": "http://p1.music.126.net/cover.jpg"},
}


def make_client(handler, credential_directory=None):
    service = SimpleNamespace(
        ensure_running=lambda: None, base_url="http://127.0.0.1:3000", token="local-token"
    )
    return NeteaseClient(
        service, transport=httpx.MockTransport(handler), credential_directory=credential_directory
    )


@pytest.mark.parametrize(('body', 'message'), [
    ({'code': 502, 'upstream_code': -460}, '网络环境存在风险'),
    ({'code': 502, 'upstream_code': 403}, '网易云拒绝本次请求（错误码 403）'),
    ({'code': 502, 'upstream_code': 'unsafe'}, 'HTTP 502'),
])
def test_gateway_reports_safe_upstream_error_codes(body, message):
    client = make_client(lambda request: httpx.Response(502, json=body))
    try:
        with pytest.raises(NeteaseError) as exc:
            client.playback_audio('1')
        assert message in str(exc.value)
    finally:
        client.close()


@pytest.mark.parametrize(('status', 'body', 'error', 'message'), [
    (502, {'code': 502, 'failure_kind': 'network'}, NeteaseNetworkError, '网络中断或超时'),
    (502, {'code': 502, 'upstream_code': 301}, NeteaseSessionExpired, '重新扫码登录'),
    (200, {'code': 8821}, NeteaseError, '拒绝二维码登录'),
    (200, {'code': -460}, NeteaseError, '网络环境存在风险'),
])
def test_qr_errors_keep_their_cause(status, body, error, message):
    client = make_client(lambda request: httpx.Response(status, json=body))
    try:
        with pytest.raises(error, match=message):
            client.check_qr('key')
    finally:
        client.close()


@pytest.mark.parametrize(('item_code', 'fee', 'message'), [
    (-110, 1, '可能需要会员'),
    (-110, 4, '可能需要购买歌曲'),
    (-110, 0, '请检查歌曲权限'),
    (401, 0, '重新扫码登录'),
    (-460, 0, '网络环境存在风险'),
    (404, 0, '未提供该歌曲的播放地址'),
])
def test_playback_no_url_distinguishes_known_outcomes(item_code, fee, message):
    client = make_client(lambda request: httpx.Response(200, json={
        'code': 200, 'data': [{'id': 1, 'code': item_code, 'url': None, 'fee': fee}],
    }))
    try:
        with pytest.raises(NeteaseError, match=message):
            client.playback_audio('1')
    finally:
        client.close()


def test_gateway_forwards_only_numeric_upstream_code(tmp_path):
    node = SERVICE_DIR / 'runtime' / ('node.exe' if os.name == 'nt' else 'node')
    if not node.is_file():
        pytest.skip('Bundled Node runtime is unavailable')
    bundle = tmp_path / 'bundle'
    (bundle / 'runtime').mkdir(parents=True)
    try:
        (bundle / 'runtime' / node.name).symlink_to(node)
    except OSError:
        pytest.skip('Cannot link the bundled Node runtime')
    shutil.copyfile(SERVICE_DIR / 'server.cjs', bundle / 'server.cjs')
    shutil.copyfile(SERVICE_DIR / 'xeapi-key.cjs', bundle / 'xeapi-key.cjs')
    upstream = bundle / 'node_modules/@neteasecloudmusicapienhanced/api'
    (upstream / 'util').mkdir(parents=True)
    (upstream / 'module').mkdir()
    (upstream / 'package.json').write_text('{}')
    (upstream / 'util/request.js').write_text('''
global.qrCalls = {};
global.qrIps = {};
module.exports = async (uri, data, options) => {
  if (uri === '/api/song/enhance/player/url/v1')
    return {body: {forwardedIp: options.ip, randomCNIP: options.randomCNIP, crypto: options.crypto}};
  const key = data.key;
  global.qrCalls[key] = (global.qrCalls[key] || 0) + 1;
  global.qrIps[key] = options.ip || null;
  if (key === 'network' && global.qrCalls[key] === 1)
    throw {status: 502, body: {code: 502, msg: 'read ECONNRESET secret'}};
  if (key === 'offline') throw {status: 502, body: {code: 502, msg: 'connect ETIMEDOUT secret'}};
  if (key === 'risk') throw {status: 400, body: {code: -460, message: 'secret'}};
  return {body: {code: 801}, cookie: []};
};
''')
    (upstream / 'util/index.js').write_text('''
exports.cookieToJson = () => ({});
exports.generateRandomChineseIP = () => '203.0.113.42';
exports.generateDeviceId = () => 'test-device';
''')
    (upstream / 'util/xeapiKey.js').write_text('''
const fs = require('node:fs');
exports.getXeapiPublicKey = async (_, deviceId) => {
  if (fs.existsSync('key-failure')) throw new Error('unavailable');
  return {sk: 'test-key', deviceId};
};
''')
    (upstream / 'util/option.js').write_text('module.exports = (query) => ({cookie: query.cookie})')
    modules = ['login_qr_key', 'login_qr_create', 'login_qr_check', 'login_status', 'logout',
               'song_detail', 'lyric', 'lyric_new', 'song_url_v1', 'cloudsearch']
    script = '''
module.exports = async (query, request) => {
  if (query.id === '1') throw {status: 400, body: {code: -460, message: 'secret'}, cookie: ['secret']};
  if (query.id === '2') throw {body: {code: '-460', message: 'secret'}};
  if (query.id === '3') throw {body: {code: 'secret'}};
  if (query.id === '5') return {body: {counts: global.qrCalls, qrIps: global.qrIps}};
  if (query.id === '6') return request('/api/song/enhance/player/url/v1', {},
                                      {randomCNIP: query.randomCNIP, crypto: query.crypto});
  throw new Error('secret');
};
'''
    for name in modules:
        (upstream / 'module' / f'{name}.js').write_text(script)
    service = NeteaseService(53002, tmp_path / 'data', bundle=bundle)
    try:
        service.ensure_running()
        with httpx.Client(trust_env=False) as http:
            for identifier, expected in [('1', {'code': 502, 'upstream_code': -460}),
                                         ('2', {'code': 502, 'upstream_code': -460}),
                                         ('3', {'code': 502}), ('4', {'code': 502})]:
                response = http.post(service.base_url + '/song/url/v1',
                                     headers={'Authorization': f'Bearer {service.token}'},
                                     json={'id': identifier})
                assert response.status_code == 502 and response.json() == expected
                assert 'secret' not in response.text
            headers = {'Authorization': f'Bearer {service.token}'}
            for key, expected in [('network', {'code': 801, 'cookie': ''}),
                                  ('risk', {'code': 502, 'upstream_code': -460}),
                                  ('offline', {'code': 502, 'failure_kind': 'network'})]:
                response = http.post(service.base_url + '/login/qr/check', headers=headers, json={'key': key})
                assert response.json() == expected
                assert 'secret' not in response.text
            response = http.post(service.base_url + '/song/url/v1', headers=headers, json={'id': '5'})
            assert response.json()['counts'] == {'network': 2, 'risk': 1, 'offline': 2}
            assert response.json()['qrIps'] == {'network': None, 'risk': None, 'offline': None}
            response = http.post(service.base_url + '/song/url/v1', headers=headers, json={'id': '6'})
            assert response.json() == {'forwardedIp': '203.0.113.42', 'randomCNIP': True, 'crypto': 'xeapi'}
    finally:
        service.close()
    failed_data = tmp_path / 'failed-data'
    failed_data.mkdir()
    (failed_data / 'key-failure').touch()
    service = NeteaseService(53002, failed_data, bundle=bundle)
    try:
        service.ensure_running()
        with httpx.Client(trust_env=False) as http:
            response = http.post(service.base_url + '/song/url/v1',
                                 headers={'Authorization': f'Bearer {service.token}'}, json={'id': '6'})
            assert response.json() == {'forwardedIp': '203.0.113.42', 'randomCNIP': True, 'crypto': 'eapi'}
    finally:
        service.close()


@pytest.mark.parametrize(
    "value",
    [
        "186016",
        "https://music.163.com/song?id=186016&userid=99",
        "https://music.163.com/#/song?id=186016",
        "https://y.music.163.com/m/song?id=186016",
        "https://music.163.com/song/id/186016/",
        "分享歌曲 https://music.163.com/song?id=186016 来自网易云",
    ],
)
def test_exact_song_links(value):
    assert song_id(value) == "186016"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "-1",
        "晴天",
        "https://evil.test/song?id=186016",
        "https://music.163.com/playlist?id=186016",
        "https://music.163.com/song?id=bad",
    ],
)
def test_invalid_links_do_not_turn_into_searches(value):
    with pytest.raises(NeteaseError):
        song_id(value)


@pytest.mark.parametrize("port", [0, 65536, -1, "3000", True, 3.1])
def test_invalid_port(port):
    with pytest.raises(NeteaseError):
        validate_port(port)


def test_occupied_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        with pytest.raises(NeteaseError, match="被占用"):
            check_port(sock.getsockname()[1])


def test_port_persistence_and_corrupt_recovery(tmp_path):
    store = Store(tmp_path)
    assert store.data["netease_port"] == 3000
    store.data["netease_port"] = 43001
    store.save()
    assert Store(tmp_path).data["netease_port"] == 43001
    store.data["netease_port"] = True
    store.save()
    assert Store(tmp_path).data["netease_port"] == 3000
    assert list(tmp_path.glob("state.broken*.json"))


def test_detail_and_search_share_the_login_session():
    calls = []

    def handler(request):
        data = json.loads(request.content)
        calls.append((request.url.path, data))
        assert request.headers["authorization"] == "Bearer local-token"
        if request.url.path == "/login/status":
            return httpx.Response(200, json={"data": {"profile": {"userId": 1, "nickname": "Listener"}}})
        if request.url.path == "/song/detail":
            return httpx.Response(200, json={"code": 200, "songs": [SONG]})
        return httpx.Response(200, json={"code": 200, "result": {"songs": [SONG], "songCount": 40}})

    client = make_client(handler)
    try:
        assert client.finish_login("MUSIC_U=session; foo=bar")["nickname"] == "Listener"
        song = client.detail("186016")
        assert (song.name, song.artists, song.album) == ("晴天", "周杰伦 / 合唱", "叶惠美")
        songs, total = client.search("晴天", 10)
        assert songs == [song] and total == 40
        assert all(data["cookie"] == "MUSIC_U=session; foo=bar" for _, data in calls)
        assert calls[-1][1]["offset"] == 10
        assert calls[-1][1]["limit"] == 10
    finally:
        client.close()


def test_legacy_song_shape():
    song = NeteaseSong.parse(
        {"id": 1, "name": "旧格式", "artists": [{"name": "歌手"}], "album": {"name": "专辑", "picUrl": ""}}
    )
    assert song.artists == "歌手" and song.album == "专辑"


@pytest.mark.parametrize(
    "body, message",
    [
        ({"code": 301}, "重新扫码登录"),
        ({"code": 200, "songs": []}, "未找到"),
        ({"code": 200, "songs": {}}, "格式发生变化"),
        ({"songs": [{"id": 2, "name": "另一个版本"}]}, "不一致"),
        ({"songs": [{"id": 186016}]}, "格式发生变化"),
    ],
)
def test_detail_failures_are_actionable(body, message):
    client = make_client(lambda request: httpx.Response(200, json=body))
    try:
        with pytest.raises(NeteaseError, match=message):
            client.detail("186016")
    finally:
        client.close()


def test_empty_search_and_malformed_response():
    client = make_client(lambda request: httpx.Response(200, json={"result": {"songCount": 0}}))
    try:
        assert client.search("不存在") == ([], 0)
        with pytest.raises(NeteaseError, match="请输入"):
            client.search(" ")
    finally:
        client.close()
    client = make_client(lambda request: httpx.Response(200, content=b"not JSON"))
    try:
        with pytest.raises(NeteaseError, match="格式发生变化"):
            client.detail("186016")
    finally:
        client.close()


def test_shortlink_resolution_keeps_credentials_local():
    def handler(request):
        if request.url.host == "163cn.tv":
            assert "authorization" not in request.headers
            assert "cookie" not in request.headers
            return httpx.Response(302, headers={"location": "https://music.163.com/song?id=186016"})
        assert request.url.host == "127.0.0.1"
        return httpx.Response(200, json={"songs": [SONG]})

    client = make_client(handler)
    client.cookie = "MUSIC_U=secret"
    try:
        assert client.detail("分享 https://163cn.tv/test 来自网易云").id == "186016"
    finally:
        client.close()


def test_cover_and_shortlink_unknown_hosts_rejected():
    client = make_client(lambda request: httpx.Response(302, headers={"location": "https://evil.test/"}))
    try:
        with pytest.raises(NeteaseError, match="未知地址"):
            client.detail("https://163cn.tv/test")
        with pytest.raises(NeteaseError, match="封面地址"):
            client.cover(replace(NeteaseSong.parse(SONG), cover_url="http://127.0.0.1/private"))
    finally:
        client.close()


def test_cover_is_https_bounded_and_separate_from_session():
    def handler(request):
        assert request.url.scheme == "https"
        assert request.url.params["param"] == "512y512"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, content=b"image")

    client = make_client(handler)
    client.cookie = "MUSIC_U=secret"
    try:
        assert client.cover(NeteaseSong.parse(SONG)) == b"image"
    finally:
        client.close()
    client = make_client(lambda request: httpx.Response(200, content=b"x" * (8 * 1024 * 1024 + 1)))
    try:
        with pytest.raises(NeteaseError, match="超过 8 MB"):
            client.cover(NeteaseSong.parse(SONG))
    finally:
        client.close()


def test_cancelled_login_and_failed_logout_clear_or_restore_session():
    client = make_client(lambda request: httpx.Response(200, json={"data": {"profile": {"userId": 1}}}))
    try:
        with pytest.raises(NeteaseError, match="已取消"):
            client.finish_login("MUSIC_U=secret", cancelled=lambda: True)
        assert not client.cookie and not client.user
        client.cookie, client.user = "MUSIC_U=secret", {"userId": 1}
        client.http.close()
        client.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(502)))
        with pytest.raises(NeteaseError):
            client.logout()
        assert not client.cookie and not client.user
    finally:
        client.close()


def test_missing_bundle_fails_clearly(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    service = NeteaseService(port, tmp_path / "data", bundle=tmp_path / "missing")
    try:
        with pytest.raises(NeteaseError, match="文件缺失"):
            service.ensure_running()
    finally:
        service.close()


def test_bundled_service_lifecycle_auth_and_restart(tmp_path):
    if not (SERVICE_DIR / "runtime/runtime.json").exists():
        pytest.skip("Run scripts/prepare_netease.py for the bundled runtime integration test")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    service = NeteaseService(port, tmp_path)
    try:
        # Concurrent editors must start exactly one process.
        errors = []

        def start():
            try:
                service.ensure_running()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=start) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert not errors and service.process.poll() is None
        original = service.process
        with httpx.Client(trust_env=False, timeout=3) as http:
            assert http.get(service.base_url + "/health").status_code == 401
            headers = {"Authorization": f"Bearer {service.token}"}
            assert http.get(service.base_url + "/health", headers=headers).json()["version"] == "4.41.1"
            assert http.post(service.base_url + "/unknown", headers=headers, json={}).status_code == 404
        original.terminate()
        original.wait(timeout=3)
        service.ensure_running()
        assert service.process.pid != original.pid
        process = service.process
        service.close()
        assert process.poll() is not None
        check_port(port)
        with pytest.raises(NeteaseError, match="已退出"):
            service.ensure_running()
    finally:
        service.close()


def test_busy_preferred_port_uses_temporary_port_and_next_launch_returns_to_preferred(tmp_path):
    if not (SERVICE_DIR / "runtime/runtime.json").exists():
        pytest.skip("Run scripts/prepare_netease.py for the bundled runtime integration test")
    with socket.socket() as occupied:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", 0))
        preferred = occupied.getsockname()[1]
        store = Store(tmp_path)
        store.data["netease_port"] = preferred
        store.save()
        service = NeteaseService(preferred, tmp_path / "netease")
        try:
            service.ensure_running()
            assert service.port != preferred and service.preferred_port == preferred
            assert Store(tmp_path).data["netease_port"] == preferred
            process = service.process
            service.configure(preferred)
            assert service.process is process and process.poll() is None
            with httpx.Client(trust_env=False) as http:
                reply = http.get(service.base_url + "/health", headers={"Authorization": f"Bearer {service.token}"})
                assert reply.json()["service"] == "AutoSkyPlay.Netease"
        finally:
            service.close()
    restarted = NeteaseService(Store(tmp_path).data["netease_port"], tmp_path / "netease")
    try:
        restarted.ensure_running()
        assert restarted.port == preferred
    finally:
        restarted.close()


def test_service_retries_if_port_is_claimed_between_probe_and_start(tmp_path, monkeypatch):
    if not (SERVICE_DIR / "runtime/runtime.json").exists():
        pytest.skip("Run scripts/prepare_netease.py for the bundled runtime integration test")
    with socket.socket() as occupied:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", 0))
        preferred = occupied.getsockname()[1]
    service = NeteaseService(preferred, tmp_path / "netease")
    start = service._start_process
    ports = []
    with socket.socket() as competitor:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            competitor.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)

        def claim_and_start(node):
            ports.append(service.port)
            if len(ports) == 1:
                competitor.bind(("127.0.0.1", service.port))
            return start(node)

        monkeypatch.setattr(service, "_start_process", claim_and_start)
        try:
            service.ensure_running()
            assert ports[0] == preferred and len(ports) == 2
            assert service.port != preferred
            assert service.process.poll() is None
        finally:
            service.close()


def test_login_survives_new_client_and_logout_removes_credentials(tmp_path, credential_store):
    from autoskyplay.auth import load_netease_auth

    calls = []

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"data": {"profile": {"userId": 1, "nickname": "Listener"}}})

    first = make_client(handler, tmp_path)
    first.finish_login("MUSIC_U=saved; __csrf=token")
    assert first.remembered
    first.close()
    assert load_netease_auth(tmp_path) == "MUSIC_U=saved; __csrf=token"
    second = make_client(handler, tmp_path)
    try:
        assert second.restore_login()["nickname"] == "Listener"
        assert second.cookie == "MUSIC_U=saved; __csrf=token" and second.remembered
        assert calls[-1][0] == "/login/status"
        assert calls[-1][1]["cookie"] == second.cookie
        second.logout()
        assert not second.remembered and not second.user
        assert load_netease_auth(tmp_path) == ""
    finally:
        second.close()


def test_expired_session_is_removed_but_network_failure_is_preserved(tmp_path, credential_store):
    from autoskyplay.auth import load_netease_auth, save_netease_auth
    from autoskyplay.netease import NeteaseSessionExpired

    save_netease_auth(tmp_path, "MUSIC_U=saved")
    client = make_client(lambda request: httpx.Response(502), tmp_path)
    try:
        with pytest.raises(NeteaseError):
            client.restore_login()
        assert client.remembered and load_netease_auth(tmp_path) == "MUSIC_U=saved"
        assert not client.cookie and not client.user
    finally:
        client.close()
    client = make_client(lambda request: httpx.Response(200, json={"data": {"profile": None}}), tmp_path)
    try:
        with pytest.raises(NeteaseSessionExpired):
            client.restore_login()
        assert not client.remembered and load_netease_auth(tmp_path) == ""
    finally:
        client.close()


def test_changed_status_shape_does_not_delete_session(tmp_path, credential_store):
    from autoskyplay.auth import load_netease_auth, save_netease_auth

    save_netease_auth(tmp_path, "MUSIC_U=saved")
    client = make_client(lambda request: httpx.Response(200, json={"data": None}), tmp_path)
    try:
        with pytest.raises(NeteaseError, match="格式发生变化"):
            client.restore_login()
        assert client.remembered and load_netease_auth(tmp_path) == "MUSIC_U=saved"
    finally:
        client.close()


def test_credential_write_failure_keeps_current_login_usable(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("local credential directory is not writable")

    monkeypatch.setattr("autoskyplay.auth._write_credential", fail)
    client = make_client(
        lambda request: httpx.Response(200, json={"data": {"profile": {"userId": 1}}}), tmp_path
    )
    try:
        assert client.finish_login("MUSIC_U=saved")["userId"] == 1
        assert client.cookie == "MUSIC_U=saved" and not client.remembered
        assert "无法保存" in client.auth_warning
    finally:
        client.close()
