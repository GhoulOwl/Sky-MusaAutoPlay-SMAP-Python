// Obtain the upstream xeapi key in a disposable, parent-time-limited process.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const upstream = path.dirname(require.resolve('@neteasecloudmusicapienhanced/api/package.json'));
const { getXeapiPublicKey } = require(path.join(upstream, 'util/xeapiKey.js'));
const target = path.join(os.tmpdir(), 'xeapi_public_key');
const temporary = `${target}.${process.pid}.tmp`;

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', async () => {
  try {
    const { deviceId } = JSON.parse(input);
    if (typeof deviceId !== 'string' || !deviceId) throw new Error('Invalid device');
    const key = await getXeapiPublicKey({}, deviceId);
    if (typeof key?.sk !== 'string' || !key.sk) throw new Error('Invalid public key');
    fs.writeFileSync(temporary, JSON.stringify(key), { mode: 0o600 });
    fs.renameSync(temporary, target);
    process.exit(0);
  } catch (_) {
    try { fs.unlinkSync(temporary); } catch (_) {}
    process.exit(1);
  }
});
