// Narrow, authenticated loopback adapter around the pinned upstream modules.
// Keep credentials and upstream responses out of logs.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const cache = path.join(process.cwd(), 'cache');
fs.mkdirSync(cache, { recursive: true });
process.env.TMPDIR = process.env.TEMP = process.env.TMP = cache;
process.env.NETEASE_COOKIE = '';
const anonymous = path.join(cache, 'anonymous_token');
if (!fs.existsSync(anonymous)) fs.writeFileSync(anonymous, '', { mode: 0o600 });
const upstream = path.dirname(require.resolve('@neteasecloudmusicapienhanced/api/package.json'));
const request = require(path.join(upstream, 'util/request.js'));
const { cookieToJson, generateRandomChineseIP, generateDeviceId } = require(path.join(upstream, 'util/index.js'));
const createOption = require(path.join(upstream, 'util/option.js'));
// Upstream 4.41.1's QR check catch block refers to an out-of-scope variable,
// masking rejected requests. Keep the response contract and let failures propagate.
async function checkQr(query, sendRequest) {
  const result = await sendRequest('/api/login/qrcode/client/login', { key: query.key, type: 3 }, createOption(query));
  return { status: 200, body: { ...result.body, cookie: result.cookie.join(';') }, cookie: result.cookie };
}
function isNetworkFailure(error) {
  if (!error || error.status !== 502 || !error.body || Number(error.body.code) !== 502) return false;
  const msg = error.body.msg;
  return typeof msg === 'string' && /\b(ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|ENETUNREACH|ENETDOWN|EHOSTUNREACH)\b|\btimeout of \d+ms exceeded\b|\bsocket hang up\b/i.test(msg);
}
async function qrNetworkRetry(uri, data, options) {
  const attempt = () => request(uri, structuredClone(data), {
    ...options, ...(options.cookie && typeof options.cookie === 'object' ? { cookie: { ...options.cookie } } : {}),
  });
  try {
    return await attempt();
  } catch (error) {
    if (!isNetworkFailure(error)) throw error;
    await new Promise(resolve => setTimeout(resolve, 250));
    return attempt();
  }
}
let playbackSourceIp;
let xeapiKeyPromise;
function prepareXeapiKey() {
  if (xeapiKeyPromise) return xeapiKeyPromise;
  global.deviceId = global.deviceId || generateDeviceId();
  xeapiKeyPromise = new Promise(resolve => {
    let child;
    try {
      const env = { ...process.env };
      delete env.AUTOSKYPLAY_NETEASE_TOKEN;
      child = spawn(process.execPath, [path.join(__dirname, 'xeapi-key.cjs')], {
        stdio: ['pipe', 'ignore', 'ignore'], windowsHide: true, env,
      });
    } catch (_) {
      resolve(false);
      return;
    }
    let settled = false;
    const finish = ok => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(ok);
    };
    const timer = setTimeout(() => { child.kill(); finish(false); }, 4000);
    child.on('error', () => finish(false));
    child.on('exit', code => finish(code === 0));
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify({ deviceId: global.deviceId }));
  });
  return xeapiKeyPromise;
}
function playbackRequest(uri, data, options) {
  // The upstream HTTP server normally translates randomCNIP to options.ip.
  // This adapter calls modules directly, so apply that translation here.
  if (options.randomCNIP) {
    if (!playbackSourceIp) playbackSourceIp = generateRandomChineseIP();
    return request(uri, data, { ...options, ip: playbackSourceIp });
  }
  return request(uri, data, options);
}
const definitions = {
  '/login/qr/key': ['login_qr_key', []],
  '/login/qr/create': ['login_qr_create', ['key', 'qrimg']],
  '/login/qr/check': ['login_qr_check', ['key']],
  '/login/status': ['login_status', []],
  '/logout': ['logout', []],
  '/song/detail': ['song_detail', ['ids']],
  '/lyric': ['lyric', ['id']],
  '/lyric/new': ['lyric_new', ['id']],
  '/song/url/v1': ['song_url_v1', ['id', 'level']],
  '/cloudsearch': ['cloudsearch', ['keywords', 'type', 'limit', 'offset']],
};
const routes = Object.fromEntries(Object.entries(definitions).map(([route, [name, fields]]) =>
  [route, { fn: route === '/login/qr/check' ? checkQr : require(path.join(upstream, 'module', name + '.js')), fields }]));
const token = process.env.AUTOSKYPLAY_NETEASE_TOKEN;
if (!token) process.exit(1);
function authorized(req) {
  const expected = Buffer.from('Bearer ' + token);
  const actual = Buffer.from(req.headers.authorization || '');
  return expected.length === actual.length && crypto.timingSafeEqual(expected, actual);
}
function send(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(body));
}
const server = http.createServer(async (req, res) => {
  if (!authorized(req)) return send(res, 401, { code: 401 });
  if (req.method === 'GET' && req.url === '/health')
    return send(res, 200, { service: 'AutoSkyPlay.Netease', version: '4.41.1' });
  const route = routes[req.url];
  if (req.method !== 'POST' || !route) return send(res, 404, { code: 404 });
  try {
    let body = '';
    for await (const chunk of req) {
      body += chunk;
      if (body.length > 65536) return send(res, 413, { code: 413 });
    }
    const data = JSON.parse(body);
    const isQrNetworkRequest = ['/login/qr/key', '/login/qr/check'].includes(req.url);
    // Two bounded attempts must fit inside the parent's 20-second HTTP timeout.
    const query = { cookie: cookieToJson(data.cookie || ''), timeout: isQrNetworkRequest ? 8000 : 15000 };
    // Use the library's xeapi after preparing its key; fall back to eapi only
    // if key preparation itself fails, never when NetEase rejects a request.
    if (req.url === '/song/url/v1') {
      query.crypto = await prepareXeapiKey() ? 'xeapi' : 'eapi';
      query.timeout = 12000;
      query.randomCNIP = true;
    }
    for (const field of route.fields) if (data[field] !== undefined) query[field] = data[field];
    const sendRequest = isQrNetworkRequest ? qrNetworkRetry : req.url === '/song/url/v1' ? playbackRequest : request;
    const result = await route.fn(query, sendRequest);
    if (!result || !result.body) throw new Error('Invalid upstream response');
    send(res, 200, result.body);
  } catch (error) {
    // Upstream may reject with an answer object, including sensitive cookies
    // or response text. Forward only a bounded numeric code, never the object.
    if (isNetworkFailure(error)) return send(res, 502, { code: 502, failure_kind: 'network' });
    const code = error && error.body && error.body.code;
    const upstreamCode = typeof code === 'number' ? code :
      (typeof code === 'string' && /^-?\d{1,5}$/.test(code) ? Number(code) : NaN);
    if (Number.isInteger(upstreamCode) && Math.abs(upstreamCode) <= 99999)
      return send(res, 502, { code: 502, upstream_code: upstreamCode });
    send(res, 502, { code: 502 });
  }
});
server.on('error', () => process.exit(1));
server.listen(Number(process.env.AUTOSKYPLAY_NETEASE_PORT), '127.0.0.1');
const parent = Number(process.env.AUTOSKYPLAY_PARENT_PID);
if (!Number.isInteger(parent) || parent <= 0) process.exit(1);
setInterval(() => {
  try { process.kill(parent, 0); } catch (_) { process.exit(0); }
}, 2000).unref();
process.on('SIGTERM', () => { server.close(); process.exit(0); });
process.on('SIGINT', () => { server.close(); process.exit(0); });
