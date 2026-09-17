import * as VKPLModule from "vklive-message-client";

const VKPLMessageClient =
  VKPLModule.default || VKPLModule.VKPLMessageClient || VKPLModule;

const input = (process.argv[2] || '').trim();
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
    `${username}|${Date.now()}|${text}`;

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

  // Keep the process alive; the client owns the WebSocket.
  await new Promise(() => {});
}

main().catch((err) => {
  console.error(String(err?.stack || err));
  process.exit(1);
});
