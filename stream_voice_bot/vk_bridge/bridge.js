import { createHash } from "node:crypto";
import * as VKPLModule from "vklive-message-client";

const VKPLMessageClient = [
  VKPLModule?.default?.default,
  VKPLModule?.default,
  VKPLModule?.VKPLMessageClient,
  VKPLModule?.default?.VKPLMessageClient,
  VKPLModule,
].find((value) => typeof value === 'function');

const input = (process.argv[2] || '').trim();
const selfTest = input === '--self-test';
if (!input) {
  console.error('Missing VK Video Live channel slug/url');
  process.exit(2);
}

function normalizeChannel(value) {
  let s = String(value).trim();
  s = s.replace(/^https?:\/\//i, '');
  if (s.includes('/')) s = s.split('/')[1] || '';
  s = s.split('?')[0].split('#')[0].replace(/^\/+|\/+$/g, '');
  return s;
}

const channel = normalizeChannel(input);
if (!channel) {
  console.error('Invalid VK Video Live channel');
  process.exit(2);
}

function emitStatus(message) {
  process.stdout.write(JSON.stringify({
    type: 'status',
    connected: true,
    message,
    channel
  }) + '\n');
}

function emitChat(ctx) {
  const text = String(ctx?.message?.text || '').trim();
  if (!text) return;

  const username =
    ctx?.user?.displayName ||
    ctx?.user?.name ||
    ctx?.user?.nick ||
    'unknown';

  const rawId =
    ctx?.message?.id ??
    ctx?.message?.rawId ??
    createHash('sha256')
      .update(JSON.stringify({
        createdAt: ctx?.message?.createdAt ?? null,
        message: ctx?.message ?? null,
        user: {
          id: ctx?.user?.id ?? null,
          nick: ctx?.user?.nick ?? username
        }
      }))
      .digest('hex')
      .slice(0, 32);

  process.stdout.write(JSON.stringify({
    type: 'chat',
    id: String(rawId),
    username,
    text,
    created_at: ctx?.message?.createdAt || Date.now() / 1000
  }) + '\n');
}

async function main() {
  if (typeof VKPLMessageClient !== 'function') {
    throw new Error(
      `vklive-message-client export is not a constructor (exports: ${Object.keys(VKPLModule).join(', ')})`
    );
  }

  if (selfTest) {
    const probe = new VKPLMessageClient({
      auth: 'readonly',
      channels: ['ci-smoke'],
      debugLog: false,
    });
    const socketFieldPresent = Object.prototype.hasOwnProperty.call(probe, 'socket');
    console.log(JSON.stringify({
      ok: true,
      constructor: VKPLMessageClient.name || 'anonymous',
      socketFieldPresent,
    }));
    return;
  }

  // Read-only mode is enough for receiving the channel chat.
  const client = new VKPLMessageClient({
    auth: 'readonly',
    channels: [channel],
    debugLog: true
  });

  client.on('message', emitChat);
  client.on('stream-status', (ctx) => {
    emitStatus(`stream-status: ${ctx?.type || 'changed'}`);
  });
  client.on('channel-info', () => {
    emitStatus('VK Video Live channel connected');
  });
  client.on('error', (err) => {
    console.error(String(err?.stack || err));
  });

  emitStatus(`Connecting to https://live.vkvideo.ru/${channel}`);
  await client.connect();
  emitStatus(`VK Video Live connected: ${channel}`);

  // The upstream client retries only after a WebSocket close. A socket can
  // remain open while the connection is effectively dead, especially during
  // long streams. Use the underlying ws object as a watchdog and let the
  // Python supervisor restart the whole bridge when the socket stays stuck.
  let trackedSocket = null;
  let lastPongAt = 0;
  let nonOpenSince = null;
  setInterval(() => {
    try {
      const socket = client?.socket;
      if (!socket) return;

      if (socket !== trackedSocket) {
        trackedSocket = socket;
        lastPongAt = Date.now();
        nonOpenSince = null;
        if (typeof socket.on === 'function') {
          socket.on('pong', () => {
            if (socket === trackedSocket) lastPongAt = Date.now();
          });
        }
      }

      const OPEN = 1;
      if (socket.readyState === OPEN) {
        nonOpenSince = null;
        if (typeof socket.ping === 'function') {
          socket.ping();
        }
        if (Date.now() - lastPongAt > 90000) {
          console.error('[watchdog] VK WebSocket opened but no pong for >90s; restarting bridge');
          process.exit(20);
        }
        return;
      }

      if (nonOpenSince === null) nonOpenSince = Date.now();
      if (Date.now() - nonOpenSince > 90000) {
        console.error('[watchdog] VK WebSocket not open for >90s; restarting bridge');
        process.exit(21);
      }
    } catch (err) {
      console.error('[watchdog] error:', String(err?.stack || err));
      process.exit(22);
    }
  }, 30000);

  // Keep the process alive; the client owns the WebSocket.
  await new Promise(() => {});
}

main().catch((err) => {
  console.error(String(err?.stack || err));
  process.exit(1);
});
