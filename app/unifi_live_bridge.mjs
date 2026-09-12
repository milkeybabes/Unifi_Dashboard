// Private loopback helper. Python launches this and proxies browser WebSockets.
import fs from 'node:fs';
import http from 'node:http';
import { ProtectClient } from 'unifi-protect';
import { WebSocketServer, WebSocket } from 'ws';

const cfg = {};
for (const raw of fs.readFileSync(process.env.UNIFI_CONFIG, 'utf8').split(/\r?\n/)) {
  const line = raw.trim();
  if (!line || /^[#;]/.test(line)) continue;
  const pos = line.indexOf('=');
  if (pos >= 0) cfg[line.slice(0, pos).trim().toLowerCase()] = line.slice(pos + 1).trim();
}
const token = process.env.UNIFI_BRIDGE_TOKEN;
if (!token) throw new Error('Launch this helper through unifi_dashboard_v3.py.');
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (req.headers.authorization !== `Bearer ${token}`) {
    res.writeHead(403); res.end(); return;
  }

  const match = /^\/snapshot\/([a-zA-Z0-9-]+)$/.exec(url.pathname);
  if (req.method === 'GET' && match) {
    try {
      const client = await getClient();
      const camera = Array.from(client.cameras).find(c => c.id === match[1]);
      if (!camera?.isOnline) { res.writeHead(503); res.end(); return; }

      const packageCamera = url.searchParams.get('lens') === '1';
      if (packageCamera && !camera.config?.featureFlags?.hasPackageCamera) {
        res.writeHead(404); res.end(); return;
      }

      const image = await camera.snapshot({ packageCamera });
      res.writeHead(200, {
        'Content-Type': 'image/jpeg',
        'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
      });
      res.end(image);
      return;
    } catch {
      res.writeHead(502); res.end(); return;
    }
  }

  res.writeHead(404); res.end();
});
const wss = new WebSocketServer({ noServer: true, perMessageDeflate: false, maxPayload: 1024 });
let clientPromise;
function getClient() {
  if (!cfg.protect_user || !cfg.protect_password) throw new Error('Protect credentials missing');
  return clientPromise ??= ProtectClient.connect({
    host: cfg.deviceip, username: cfg.protect_user, password: cfg.protect_password,
  }).catch(error => { clientPromise = undefined; throw error; });
}
server.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url, 'http://localhost');
  if (req.headers.authorization !== `Bearer ${token}` || !/^\/live\/[a-zA-Z0-9-]+$/.test(url.pathname)) {
    socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n'); return;
  }
  const lens = url.searchParams.get('lens') === '1' ? 1 : 0;
  wss.handleUpgrade(req, socket, head, ws => wss.emit('connection', ws, {
    id: url.pathname.split('/').pop(),
    lens,
  }));
});
wss.on('connection', async (ws, requestInfo) => {
  const { id, lens } = requestInfo;
  const abort = new AbortController();
  let live, init, last = Date.now(), stage = 'Protect login failed';
  ws.on('close', () => abort.abort());
  ws.on('error', () => abort.abort());
  const watchdog = setInterval(() => {
    if (Date.now() - last > 15000) { abort.abort(); ws.close(1011, 'Stream stalled'); }
  }, 2000);
  const send = data => new Promise((resolve, reject) => {
    if (ws.readyState !== WebSocket.OPEN || ws.bufferedAmount > 8 * 1024 * 1024) {
      reject(new Error('Viewer disconnected or too slow')); return;
    }
    ws.send(data, error => error ? reject(error) : resolve());
  });
  try {
    await send(JSON.stringify({type:'status', message:'CONNECTING TO PROTECT'}));
    const client = await getClient();
    if (abort.signal.aborted) return;
    stage = 'Camera missing or offline in native Protect session';
    const camera = Array.from(client.cameras).find(c => c.id === id);
    if (!camera?.isOnline) throw new Error('Camera unavailable');

    if (lens === 1 && !camera.config?.featureFlags?.hasPackageCamera) {
      stage = 'Package camera unavailable';
      throw new Error('Package camera unavailable');
    }

    stage = lens === 1 ? 'Package camera livestream failed' : 'Protect livestream failed';
    await send(JSON.stringify({
      type:'status',
      message: lens === 1 ? 'WAITING FOR PACKAGE VIDEO' : 'WAITING FOR VIDEO'
    }));

    live = lens === 1
      // G6 Entry probe result: Protect's package-camera livestream is secondary lens 2.
      ? camera.livestream({ source: { type: 'lens', lens: 2 }, signal: abort.signal })
      : camera.livestream({ signal: abort.signal });
    for await (const segment of live) {
      last = Date.now();
      if (segment.type === 'init') {
        init = segment;
        console.log(
          lens === 1 ? 'Package view: initialization received; codec:' : 'Live view: initialization received; codec:',
          String(segment.codec).replace(/[^a-zA-Z0-9.,;= "_-]/g, '').slice(0,160)
        );
        await send(JSON.stringify({ type: 'init', codec: segment.codec }));
        await send(segment.data);
      } else if (segment.type === 'media') {
        if (!init) throw new Error('Missing initialization segment');
        // Recovery resets timestamps. Rebuild MSE before the new keyframe.
        if (segment.discontinuity) {
          await send(JSON.stringify({ type: 'init', codec: init.codec }));
          await send(init.data);
        }
        await send(segment.data);
      }
    }
  } catch (error) {
    // Report stage and exception class, never upstream messages or credentials.
    const reason = !cfg.protect_user || !cfg.protect_password ? 'Protect_User / Protect_Password missing from Unifi.ini' : stage;
    console.error(`Live view: ${reason} (${String(error?.constructor?.name || 'Error').replace(/[^a-zA-Z]/g, '')})`);
    if (ws.readyState === WebSocket.OPEN) {
      try { await send(JSON.stringify({type:'error', message:reason})); } catch {}
      ws.close(1011, 'Native stream unavailable');
    }
  } finally {
    clearInterval(watchdog);
    abort.abort();
    try { await live?.[Symbol.asyncDispose]?.(); } catch {}
    ws.close();
  }
});
server.listen(Number(process.env.UNIFI_BRIDGE_PORT), '127.0.0.1', () => console.log('Native livestream helper ready.'));
server.on('error', () => { console.error('Livestream helper could not listen.'); process.exit(1); });
// Normal dashboard shutdown terminates this child; direct signals dispose streams.
async function shutdown() {
  for (const ws of wss.clients) ws.terminate();
  try { await (await clientPromise)?.[Symbol.asyncDispose]?.(); } catch {}
  server.close();
  process.exit(0);
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
