import VKPLMessageClient from "vklive-message-client";

const input = (process.argv[2] || "").trim();
const selfTest = input === "--self-test";

function normalizeChannel(value) {
  let s = String(value || "").trim();
  s = s.replace(/^https?:\/\//i, "");
  if (s.includes("/")) {
    const parts = s.split("/").filter(Boolean);
    if (parts.length >= 2 && /(^|\.)vkvideo\.ru$/i.test(parts[0])) {
      s = parts[1];
    } else {
      s = parts[0] || "";
    }
  }
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

function isChatBotContext(ctx) {
  const user = ctx?.user || {};
  const names = [
    user.nick,
    user.displayName,
    user.name,
  ]
    .filter(Boolean)
    .map((value) => String(value).trim().toLowerCase());
  return names.includes("chatbot");
}

function mapMessageContext(ctx) {
  const text = String(ctx?.message?.text || "").trim();
  if (!text) return null;

  const user = ctx?.user || {};
  const username =
    user.nick ||
    user.displayName ||
    user.name ||
    "unknown";

  const createdAt = Number(ctx?.createdAt);
  const id = ctx?.id != null
    ? String(ctx.id)
    : [
        createdAt || 0,
        user.id || 0,
        username,
        text,
      ].join(":");

  return {
    type: "chat",
    id,
    username: String(username),
    text,
    created_at: Number.isFinite(createdAt) ? createdAt : Date.now() / 1000,
  };
}

async function main() {
  if (!input) {
    console.error("Missing VK Video Live channel slug/url");
    process.exit(2);
  }

  if (selfTest) {
    const client = new VKPLMessageClient({
      auth: "readonly",
      channels: ["__self_test__"],
      debugLog: false,
      log: false,
    });

    const serialized = client.messageParser.serialize("Тест-Тест 123");
    if (!Array.isArray(serialized) || serialized.length !== 1) {
      throw new Error("VK package parser self-test failed");
    }

    const first = serialized[0];
    if (first?.type !== "text" || !String(first.content || "").includes("Тест-Тест 123")) {
      throw new Error("VK package text serialization self-test failed");
    }

    const mapped = mapMessageContext({
      id: 42,
      createdAt: 1700000000,
      user: { id: 7, nick: "ChatBot", displayName: "ChatBot" },
      message: { text: "ChatBot: Jostik получает награду за 2: Тест-Тест 123" },
    });
    if (!mapped || mapped.username !== "ChatBot" || mapped.text !== "ChatBot: Jostik получает награду за 2: Тест-Тест 123") {
      throw new Error("VK bridge message mapping self-test failed");
    }
    if (isChatBotContext({ user: { nick: "Jostik", displayName: "Jostik" } })) {
      throw new Error("VK bridge ordinary-chat filter self-test failed");
    }
    if (!isChatBotContext({ user: { nick: "ChatBot", displayName: "ChatBot" } })) {
      throw new Error("VK bridge ChatBot filter self-test failed");
    }

    console.log(JSON.stringify({
      ok: true,
      transport: "vklive-message-client@5.3.2",
      auth: "readonly",
      channelSubscription: "public-chat:<resolved-public-websocket-channel>",
      parser: "package",
    }));
    return;
  }

  const channel = normalizeChannel(input);
  if (!channel) {
    console.error("Invalid VK Video Live channel");
    process.exit(2);
  }

  try {
    const client = new VKPLMessageClient({
      auth: "readonly",
      channels: [channel],
      debugLog: false,
      log: false,
    });

    client.on("message", (ctx) => {
      try {
        if (!isChatBotContext(ctx)) {
          return;
        }
        const chat = mapMessageContext(ctx);
        if (chat) {
          chat.is_chatbot = true;
          process.stdout.write(JSON.stringify(chat) + "\n");
        }
      } catch (error) {
        console.error("[vk] message mapping error:", String(error?.stack || error));
      }
    });

    client.on("channel-info", (ctx) => {
      emitStatus(
        true,
        "VK channel info: online=" + Boolean(ctx?.isOnline) + " viewers=" + Number(ctx?.viewers || 0),
        channel,
      );
    });

    client.on("stream-status", (ctx) => {
      emitStatus(true, "VK event: " + String(ctx?.type || "stream-status"), channel);
    });

    client.on("reconnect", () => {
      emitStatus(true, "VK Video Live chat reconnected", channel);
    });

    emitStatus(false, "Connecting to https://live.vkvideo.ru/" + channel + " via vklive-message-client", channel);
    await client.connect();
    emitStatus(true, "VK Video Live chat connected: " + channel, channel);

    await new Promise(() => {});
  } catch (error) {
    emitStatus(false, "VK bridge error: " + (error?.message || error), channel);
    console.error(String(error?.stack || error));
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(String(error?.stack || error));
  process.exit(1);
});
