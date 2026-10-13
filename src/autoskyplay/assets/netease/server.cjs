// Narrow, authenticated loopback adapter around the pinned upstream modules.
// Keep credentials and upstream responses out of logs.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const cache = path.join(process.cwd(), 'cache');
fs.mkdirSync(cache, { recursive: true });
process.env.TMPDIR = process.env.TEMP = process.env.TMP = cache;
process.env.NETEASE_COOKIE = '';
const anonymous = path.join(cache, 'anonymous_token');
if (!fs.existsSync(anonymous)) fs.writeFileSync(anonymous, '', { mode: 0o600 });
const upstream = path.dirname(require.resolve('@neteasecloudmusicapienhanced/api/package.json'));
const request = require(path.join(upstream, 'util/request.js'));
const { cookieToJson } = require(path.join(upstream, 'util/index.js'));
const definitions = {
  '/login/qr/key': ['login_qr_key', []],
  '/login/qr/create': ['login_qr_create', ['key', 'qrimg']],
  '/login/qr/check': ['login_qr_check', ['key']],
  '/login/status': ['login_status', []],
  '/logout': ['logout', []],
  '/song/detail': ['song_detail', ['ids']],
  '/cloudsearch': ['cloudsearch', ['keywords', 'type', 'limit', 'offset']],
};
const routes = Object.fromEntries(Object.entries(definitions).map(([route, [name, fields]]) =>
  [route, { fn: require(path.join(upstream, 'module', name + '.js')), fields }]));
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
    const query = { cookie: cookieToJson(data.cookie || ''), timeout: 15000 };
    for (const field of route.fields) if (data[field] !== undefined) query[field] = data[field];
    const result = await route.fn(query, request);
    if (!result || !result.body) throw new Error('Invalid upstream response');
    send(res, 200, result.body);
  } catch (_) {
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
