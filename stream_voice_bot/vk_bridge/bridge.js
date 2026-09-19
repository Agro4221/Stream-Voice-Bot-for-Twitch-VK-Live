import { createHash } from "node:crypto";
import WebSocket from "ws";

const API_BASE = "https://api.live.vkvideo.ru/v1";
const WS_URL = "wss://pubsub.live.vkvideo.ru/connection/websocket?cf_protocol_version=v2";
const input = (process.argv[2] || "").trim();
const selfTest = input === "--self-test";

function normalizeChannel(value) {
  let s = String(value).trim();
  s = s.replace(/^https?:\/\//i, "");
  if (s.includes("/")) s = s.split("/")[1] || "";
  return s.split("?")[0].split("#")[0].replace(/^\/+|\/+$/g, "");
}

function emitStatus(connected, message, channel = "") {
  process.stdout.write(JSON.stringify({
    type: "status",
    connected,
    message,
    channel,
  }) + "\n");
}

function decodeTextBlock(block) {
  const content = block?.content;
  if (typeof content !== "string") return "";
  try {
    const parsed = JSON.parse(content);
    if (Array.isArray(parsed) && parsed.length > 0) {
      return String(parsed[0] ?? "");
    }
  } catch {
    // Older/current payload variants may carry plain text directly.
  }
  return content;
}

function extractMessageText(blocks) {
  if (!Array.isArray(blocks)) return "";
  const parts = [];
  for (const block of blocks) {
    if (!block || typeof block !== "object") continue;
    if (block.type === "text") {
      const value = decodeTextBlock(block);
      if (value) parts.push(value);
    } else if (block.type === "mention") {
      const name = block.displayName || block.name || block.nick;
      if (name) parts.push(String(name));
    } else if (block.type === "link") {
      const value = block.content || block.url;
      if (value) parts.push(String(value));
    } else if (block.type === "smile") {
      const name = block.name;
      if (name) parts.push(String(name));
    }
  }
  return parts.join("").trim();
}

function parseChatPush(payload) {
  const data = payload?.push?.pub?.data;
  if (!data || data.type !== "message") return null;

  const body = data.data;
  const text = extractMessageText(body?.data);
  if (!text) return null;

  const author = body?.author || body?.user || {};
  const username =
    author.displayName ||
    author.name ||
    author.nick ||
    "unknown";

  const id = body?.id ?? createHash("sha256")
    .update(JSON.stringify({
      createdAt: body?.createdAt ?? null,
      blocks: body?.data ?? null,
      authorId: author?.id ?? null,
      username,
    }))
    .digest("hex")
    .slice(0, 32);

  return {
    type: "chat",
    id: String(id),
    username: String(username),
    text,
    created_at: body?.createdAt || Date.now() / 1000,
  };
}

async function getJson(url) {
  const response = await fetch(url, {
    headers: {
      Accept: "application/json",
      Origin: "https://live.vkvideo.ru",
      Referer: "https://live.vkvideo.ru/",
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/127 Safari/537.36",
    },
  });
  const raw = await response.text();
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    throw new Error(`VK API returned non-JSON (HTTP ${response.status}): ${raw.slice(0, 500)}`);
  }
  if (!response.ok) {
    throw new Error(`VK API HTTP ${response.status}: ${JSON.stringify(data).slice(0, 1000)}`);
  }
  return data;
}

async function resolveChannel(channel) {
  // The current VK Video Live client resolves the public websocket channel
  // from /blog/<slug>. The old /public_video_stream/chat/user/ endpoint returns
  // the owner id, which is not the same thing as the public chat subscription.
  const data = await getJson(`${API_BASE}/blog/${encodeURIComponent(channel)}`);
  const rawChannel = data?.publicWebSocketChannel ?? data?.data?.publicWebSocketChannel ?? "";
  const publicWebSocketChannel = String(rawChannel).split(":").pop();
  const ownerId = data?.owner?.id ?? data?.data?.owner?.id ?? "";
  if (!publicWebSocketChannel) {
    throw new Error(`VK public websocket chat channel not found for "${channel}"`);
  }
  return {
    publicWebSocketChannel,
    ownerId: ownerId ? String(ownerId) : "",
  };
}

async function getConnectToken() {
  const data = await getJson(`${API_BASE}/ws/connect`);
  const token = data?.token;
  if (!token) throw new Error("VK WebSocket connect token is missing");
  return String(token);
}

function sendCommand(socket, state, command) {
  const id = ++state.nextId;
  const payload = { ...command, id };
  return new Promise((resolve, reject) => {
    state.pending.set(id, { resolve, reject });
    socket.send(JSON.stringify(payload), (error) => {
      if (error) {
        state.pending.delete(id);
        reject(error);
      }
    });
  });
}

function rejectPending(state, error) {
  for (const { reject } of state.pending.values()) {
    reject(error);
  }
  state.pending.clear();
}

async function main() {
  if (!input) {
    console.error("Missing VK Video Live channel slug/url");
    process.exit(2);
  }

  if (selfTest) {
    const sample = {
      push: {
        channel: "channel-chat:123",
        pub: {
          data: {
            type: "message",
            data: {
              id: 42,
              createdAt: 1700000000,
              author: { id: 7, displayName: "Jostik" },
              data: [{ type: "text", content: JSON.stringify(["Тест-Тест 123"]) }],
            },
          },
        },
      },
    };
    const parsed = parseChatPush(sample);
    if (!parsed || parsed.username !== "Jostik" || parsed.text !== "Тест-Тест 123") {
      throw new Error("Bridge self-test parser failed");
    }
    const normalized = normalizeChannel("https://live.vkvideo.ru/example_channel?x=1");
    if (normalized !== "example_channel") {
      throw new Error("Bridge self-test channel normalization failed");
    }
    console.log(JSON.stringify({
      ok: true,
      transport: "raw-vk-websocket",
      channelSubscription: "channel-chat:<channel-id>",
      parser: "ok",
    }));
    return;
  }

  const channel = normalizeChannel(input);
  if (!channel) {
    console.error("Invalid VK Video Live channel");
    process.exit(2);
  }

  let channelId = "";
  let publicChatChannel = "";
  try {
    const resolved = await resolveChannel(channel);
    channelId = resolved.ownerId || resolved.publicWebSocketChannel;
    publicChatChannel = resolved.publicWebSocketChannel;
    const token = await getConnectToken();

    emitStatus(false, `Connecting to https://live.vkvideo.ru/${channel} (chat channel ${publicChatChannel})`, channel);

    const socket = new WebSocket(WS_URL, {
      headers: { Origin: "https://live.vkvideo.ru" },
    });

    const state = {
      nextId: 0,
      pending: new Map(),
      lastPongAt: Date.now(),
      openedAt: 0,
      subscribed: false,
    };

    socket.on("open", async () => {
      state.openedAt = Date.now();
      try {
        const connected = await sendCommand(socket, state, {
          connect: { token, name: "js" },
        });
        if (connected?.error) {
          throw new Error(`VK websocket connect failed: ${JSON.stringify(connected.error)}`);
        }

        const subscribed = await sendCommand(socket, state, {
          subscribe: { channel: `public-chat:${publicChatChannel}` },
        });
        if (subscribed?.error) {
          throw new Error(`VK chat subscribe failed: ${JSON.stringify(subscribed.error)}`);
        }

        state.subscribed = true;
        emitStatus(true, `VK Video Live chat connected: ${channel}`, channel);
      } catch (error) {
        console.error(String(error?.stack || error));
        try { socket.close(); } catch {}
      }
    });

    socket.on("message", (raw) => {
      const line = raw.toString("utf8");
      if (line === "{}") {
        socket.send("{}");
        return;
      }

      let payload;
      try {
        payload = JSON.parse(line);
      } catch {
        console.error(`[vk] Ignoring non-JSON websocket frame: ${line.slice(0, 500)}`);
        return;
      }

      if (payload && typeof payload === "object" && Number.isInteger(payload.id)) {
        const pending = state.pending.get(payload.id);
        if (pending) {
          state.pending.delete(payload.id);
          pending.resolve(payload);
          return;
        }
      }

      const chat = parseChatPush(payload);
      if (chat) {
        process.stdout.write(JSON.stringify(chat) + "\n");
        return;
      }

      const pushType = payload?.push?.pub?.data?.type;
      if (pushType === "stream_start" || pushType === "stream_end" || pushType === "stream_online_status") {
        emitStatus(true, `VK event: ${pushType}`, channel);
      }
    });

    socket.on("pong", () => {
      state.lastPongAt = Date.now();
    });

    socket.on("error", (error) => {
      console.error(String(error?.stack || error));
    });

    socket.on("close", (code, reason) => {
      const suffix = reason?.toString("utf8") ? ` reason=${reason.toString("utf8")}` : "";
      rejectPending(state, new Error(`VK WebSocket closed: code=${code}${suffix}`));
      emitStatus(false, `VK WebSocket closed: code=${code}.`, channel);
      process.exit(20);
    });

    setInterval(() => {
      try {
        if (socket.readyState !== WebSocket.OPEN) {
          if (Date.now() - state.openedAt > 30000) {
            console.error("[watchdog] VK WebSocket not open for >30s");
            process.exit(21);
          }
          return;
        }
        socket.ping();
        if (Date.now() - state.lastPongAt > 90000) {
          console.error("[watchdog] VK WebSocket opened but no pong for >90s; restarting bridge");
          process.exit(22);
        }
      } catch (error) {
        console.error("[watchdog] error:", String(error?.stack || error));
        process.exit(23);
      }
    }, 30000);

    await new Promise(() => {});
  } catch (error) {
    console.error(String(error?.stack || error));
    emitStatus(false, `VK bridge error: ${error?.message || error}`, channel);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(String(error?.stack || error));
  process.exit(1);
});
