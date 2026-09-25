from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlencode

import httpx
import websockets

from .secrets import SecretStore

log = logging.getLogger("stream_voice_bot.twitch")

API_BASE = "https://api.twitch.tv/helix"
OAUTH_AUTHORIZE = "https://id.twitch.tv/oauth2/authorize"
OAUTH_TOKEN = "https://id.twitch.tv/oauth2/token"
OAUTH_DEVICE = "https://id.twitch.tv/oauth2/device"
EVENTSUB_WS = "wss://eventsub.wss.twitch.tv/ws"


@dataclass
class TwitchIdentity:
    user_id: str
    login: str
    display_name: str
    scopes: list[str]


class TwitchService:
    REQUIRED_SCOPES = [
        "user:read:chat",
        "channel:read:redemptions",
        "channel:manage:redemptions",
    ]

    def __init__(self, db, on_chat: Callable[[dict], None], on_redemption: Callable[[dict], None], on_status: Callable[[dict], None]):
        self.db = db
        self.secrets = SecretStore()
        # Migrate legacy secrets once, so existing installations keep their OAuth session.
        for key in ("twitch_client_secret", "twitch_access_token", "twitch_refresh_token"):
            legacy = self.db.get_setting(key, "") or ""
            if legacy and not self.secrets.get(key):
                self.secrets.set(key, legacy)
                self.db.delete_setting(key)
        self.on_chat = on_chat
        self.on_redemption = on_redemption
        self.on_status = on_status
        self.task: asyncio.Task | None = None
        self.ws = None
        self.session_id: str | None = None
        self.running = False
        self.connected = False
        self.identity: TwitchIdentity | None = None
        self._refresh_lock = asyncio.Lock()

    def client_id(self) -> str:
        return self.db.get_setting("twitch_client_id", "") or ""

    def client_secret(self) -> str:
        return self.secrets.get("twitch_client_secret")

    def access_token(self) -> str:
        return self.secrets.get("twitch_access_token")

    def refresh_token(self) -> str:
        return self.secrets.get("twitch_refresh_token")

    def redirect_uri(self) -> str:
        return self.db.get_setting(
            "twitch_redirect_uri",
            "http://localhost:8787/auth/twitch/callback",
        ) or "http://localhost:8787/auth/twitch/callback"

    def configured(self) -> bool:
        return bool(self.client_id())

    def using_device_flow(self) -> bool:
        return bool(self.client_id())

    async def start_device_flow(self) -> dict:
        if not self.client_id():
            raise RuntimeError("Set Twitch Client ID first")
        data = {"client_id": self.client_id(), "scopes": " ".join(self.REQUIRED_SCOPES)}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(OAUTH_DEVICE, data=data)
            r.raise_for_status()
            payload = r.json()
        self.db.set_setting("twitch_device_code", payload.get("device_code", ""))
        self.db.set_setting("twitch_device_expires_at", str(time.time() + int(payload.get("expires_in", 0))))
        self.db.set_setting("twitch_device_interval", str(payload.get("interval", 5)))
        self.on_status({
            "connected": False,
            "device_pending": True,
            "message": f"Открой {payload.get('verification_uri')} и введи код {payload.get('user_code')}",
        })
        return payload

    async def finish_device_flow(self, device_code: str, interval: int = 5, expires_in: int = 1800) -> None:
        deadline = time.time() + max(30, expires_in)
        wait = max(1, int(interval or 5))
        async with httpx.AsyncClient(timeout=20) as client:
            while time.time() < deadline:
                r = await client.post(OAUTH_TOKEN, data={
                    "client_id": self.client_id(),
                    "scopes": " ".join(self.REQUIRED_SCOPES),
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                })
                if r.status_code == 200:
                    data = r.json()
                    self.secrets.set("twitch_access_token", data["access_token"])
                    if data.get("refresh_token"):
                        self.secrets.set("twitch_refresh_token", data["refresh_token"])
                    self.db.set_setting("twitch_scopes", json.dumps(data.get("scope", []), ensure_ascii=False))
                    self.db.set_setting("twitch_token_updated_at", str(time.time()))
                    self.db.delete_setting("twitch_device_code")
                    await self.identify()
                    await self.start()
                    self.on_status({"connected": True, "device_pending": False, "message": "Twitch подключён через Device Code Flow"})
                    return
                try:
                    err = r.json().get("message") or r.json().get("error") or "authorization_pending"
                except Exception:
                    err = "authorization_pending"
                if err == "authorization_pending":
                    await asyncio.sleep(wait)
                    continue
                if err == "slow_down":
                    wait += 5
                    await asyncio.sleep(wait)
                    continue
                if err in {"expired_token", "access_denied"}:
                    raise RuntimeError(f"Twitch Device Flow: {err}")
                r.raise_for_status()
        raise RuntimeError("Twitch Device Flow timed out")

    def authorization_url(self) -> str:
        state = secrets.token_urlsafe(32)
        self.db.set_setting("twitch_oauth_state", state)
        params = {
            "response_type": "code",
            "client_id": self.client_id(),
            "redirect_uri": self.redirect_uri(),
            "scope": " ".join(self.REQUIRED_SCOPES),
            "state": state,
        }
        return f"{OAUTH_AUTHORIZE}?{urlencode(params)}"

    async def exchange_code(self, code: str, state: str):
        expected = self.db.get_setting("twitch_oauth_state", "")
        if not expected or not secrets.compare_digest(expected, state):
            raise RuntimeError("OAuth state mismatch")

        data = {
            "client_id": self.client_id(),
            "client_secret": self.client_secret(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri(),
        }
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(OAUTH_TOKEN, data=data)
            r.raise_for_status()
            token_data = r.json()

        self.secrets.set("twitch_access_token", token_data["access_token"])
        if token_data.get("refresh_token"):
            self.secrets.set("twitch_refresh_token", token_data["refresh_token"])
        self.db.set_setting("twitch_scopes", json.dumps(token_data.get("scope", []), ensure_ascii=False))
        self.db.set_setting("twitch_token_updated_at", str(time.time()))
        await self.identify()

    async def refresh_access_token(self, stale_access_token: str | None = None):
        async with self._refresh_lock:
            current = self.access_token()
            if stale_access_token and current and current != stale_access_token:
                return current

            rt = self.refresh_token()
            if not rt:
                raise RuntimeError("No Twitch refresh token")
            params = {
                "client_id": self.client_id(),
                "grant_type": "refresh_token",
                "refresh_token": rt,
            }
            if self.client_secret():
                params["client_secret"] = self.client_secret()
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(OAUTH_TOKEN, data=params)
                r.raise_for_status()
                data = r.json()

            self.secrets.set("twitch_access_token", data["access_token"])
            if data.get("refresh_token"):
                self.secrets.set("twitch_refresh_token", data["refresh_token"])
            self.db.set_setting("twitch_scopes", json.dumps(data.get("scope", []), ensure_ascii=False))
            return data["access_token"]

    async def _api(self, method: str, path: str, *, params=None, json_data=None):
        token = self.access_token()
        if not token:
            raise RuntimeError("Twitch is not authorized")
        headers = {"Client-Id": self.client_id(), "Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.request(
                method,
                API_BASE + path,
                headers=headers,
                params=params,
                json=json_data,
            )
            if r.status_code == 401:
                token = await self.refresh_access_token(token)
                headers["Authorization"] = f"Bearer {token}"
                r = await client.request(
                    method,
                    API_BASE + path,
                    headers=headers,
                    params=params,
                    json=json_data,
                )
            r.raise_for_status()
            return r.json()

    async def validate_token(self):
        token = self.access_token()
        if not token:
            return False
        headers = {
            "Authorization": f"OAuth {token}",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://id.twitch.tv/oauth2/validate",
                headers=headers,
            )
            if r.status_code == 200:
                return True
            if r.status_code == 401 and self.refresh_token():
                await self.refresh_access_token(token)
                return True
            return False

    async def identify(self):
        data = await self._api("GET", "/users")
        users = data.get("data", [])
        if not users:
            raise RuntimeError("Twitch returned no user for this access token")
        u = users[0]
        scopes = json.loads(self.db.get_setting("twitch_scopes", "[]") or "[]")
        self.identity = TwitchIdentity(
            user_id=u["id"],
            login=u["login"],
            display_name=u["display_name"],
            scopes=scopes,
        )
        self.db.set_setting("twitch_user_id", u["id"])
        self.db.set_setting("twitch_login", u["login"])
        self.db.set_setting("twitch_display_name", u["display_name"])
        return self.identity

    async def update_custom_reward(
        self,
        reward_id: str,
        *,
        title: str | None = None,
        prompt: str | None = None,
        is_enabled: bool | None = None,
        is_user_input_required: bool | None = None,
        should_redemptions_skip_request_queue: bool | None = None,
    ):
        if not self.identity:
            await self.identify()

        body = {}
        if title is not None:
            body["title"] = title
        if prompt is not None:
            body["prompt"] = prompt
        if is_enabled is not None:
            body["is_enabled"] = is_enabled
        if is_user_input_required is not None:
            body["is_user_input_required"] = is_user_input_required
        if should_redemptions_skip_request_queue is not None:
            body["should_redemptions_skip_request_queue"] = (
                should_redemptions_skip_request_queue
            )

        if not body:
            raise ValueError("No reward fields to update")

        return await self._api(
            "PATCH",
            "/channel_points/custom_rewards",
            params={
                "broadcaster_id": self.identity.user_id,
                "id": reward_id,
            },
            json_data=body,
        )

    async def get_custom_rewards(self):
        if not self.identity:
            await self.identify()
        return await self._api(
            "GET",
            "/channel_points/custom_rewards",
            params={"broadcaster_id": self.identity.user_id},
        )

    async def get_unfulfilled_redemptions(self, reward_id: str, first: int = 50):
        if not self.identity:
            await self.identify()
        params = {
            "broadcaster_id": self.identity.user_id,
            "reward_id": reward_id,
            "status": "UNFULFILLED",
            "first": max(1, min(int(first), 50)),
        }
        items = []
        cursor = None
        while True:
            if cursor:
                params["after"] = cursor
            data = await self._api(
                "GET",
                "/channel_points/custom_rewards/redemptions",
                params=params,
            )
            items.extend(data.get("data", []))
            cursor = (data.get("pagination") or {}).get("cursor")
            if not cursor or len(items) >= 500:
                return items

    async def update_redemption(self, reward_id: str, redemption_id: str, status: str):
        if not self.identity:
            return
        return await self._api(
            "PATCH",
            "/channel_points/custom_rewards/redemptions",
            params={
                "broadcaster_id": self.identity.user_id,
                "reward_id": reward_id,
                "id": redemption_id,
                "status": status,
            },
        )

    async def create_subscription(self, sub_type: str, version: str, condition: dict):
        if not self.session_id:
            raise RuntimeError("EventSub WebSocket session is not ready")
        return await self._api(
            "POST",
            "/eventsub/subscriptions",
            json_data={
                "type": sub_type,
                "version": version,
                "condition": condition,
                "transport": {
                    "method": "websocket",
                    "session_id": self.session_id,
                },
            },
        )

    async def _subscribe(self):
        if not self.identity:
            await self.identify()
        # Only Channel Points redemptions drive TTS. Ordinary chat is
        # deliberately not subscribed here.
        response = await self.create_subscription(
            "channel.channel_points_custom_reward_redemption.add",
            "1",
            {"broadcaster_user_id": self.identity.user_id},
        )
        if not response.get("data"):
            raise RuntimeError("Twitch EventSub subscription was not created")
        return response

    async def start(self):
        if self.task and not self.task.done():
            return
        if not self.configured():
            raise RuntimeError("Set Twitch Client ID first")
        if not self.access_token():
            raise RuntimeError("Authorize Twitch first")

        self.running = True
        self.task = asyncio.create_task(self._run(), name="twitch-eventsub")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        self.connected = False
        self.session_id = None
        self.on_status({"connected": False, "message": "Остановлено"})

    async def _run(self):
        backoff = 2
        while self.running:
            try:
                await self._run_one()
                backoff = 2
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                self.on_status({"connected": False, "message": f"EventSub: {type(e).__name__}: {e}"})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _open_eventsub_socket(self, url: str):
        ws = await websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        )
        try:
            first = json.loads(await ws.recv())
            metadata = first.get("metadata", {})
            payload = first.get("payload", {})
            if metadata.get("message_type") != "session_welcome":
                await ws.close()
                raise RuntimeError(
                    f"Expected session_welcome, got {metadata.get('message_type')}"
                )
            return ws, payload["session"]
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass
            raise

    async def _run_one(self):
        ws = None
        try:
            if not self.identity:
                await self.identify()
            scopes = set(self.identity.scopes if self.identity else [])
            missing = [scope for scope in ("channel:read:redemptions",) if scope not in scopes]
            if missing:
                raise RuntimeError(
                    "Twitch OAuth token lacks required scope(s): " + ", ".join(missing) +
                    ". Reauthorize Twitch for this app."
                )

            ws, session = await self._open_eventsub_socket(EVENTSUB_WS)
            self.ws = ws
            self.session_id = session["id"]

            subscription = await self._subscribe()
            sub = (subscription.get("data") or [{}])[0]
            self.connected = True
            self.on_status({
                "connected": True,
                "message": "Twitch EventSub connected: Channel Points listening",
                "session_id": self.session_id,
                "subscription_id": sub.get("id", ""),
                "login": self.identity.login if self.identity else "",
            })

            keepalive_timeout = max(
                10,
                int(session.get("keepalive_timeout_seconds") or 10),
            )
            while self.running:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(),
                        timeout=keepalive_timeout * 2,
                    )
                except asyncio.TimeoutError as e:
                    raise RuntimeError(
                        f"Twitch EventSub keepalive timeout ({keepalive_timeout}s)"
                    ) from e
                msg = json.loads(raw)
                meta = msg.get("metadata", {})
                msg_type = meta.get("message_type")

                if msg_type == "notification":
                    event_type = meta.get("subscription_type")
                    event = msg.get("payload", {}).get("event", {})
                    # Keep Twitch's envelope message id so the app can dedupe
                    # a redelivery even when the payload itself has no id.
                    if meta.get("message_id"):
                        event = {**event, "_eventsub_message_id": meta["message_id"]}
                    if event_type == "channel.channel_points_custom_reward_redemption.add":
                        try:
                            self.on_redemption(event)
                        except Exception:
                            log.exception("Redemption event handler failed")

                elif msg_type == "session_reconnect":
                    reconnect_url = (
                        msg.get("payload", {})
                        .get("session", {})
                        .get("reconnect_url")
                    )
                    if not reconnect_url:
                        raise RuntimeError("Twitch sent session_reconnect without reconnect_url")

                    self.on_status({
                        "connected": True,
                        "message": "Twitch reconnecting EventSub session…",
                        "reconnect_url": reconnect_url,
                    })

                    # Twitch requires the new socket to receive Welcome before
                    # the old socket is closed. The reconnect URL carries the
                    # old subscriptions automatically, so do not resubscribe.
                    new_ws, new_session = await self._open_eventsub_socket(reconnect_url)
                    old_ws = ws
                    ws = new_ws
                    self.ws = new_ws
                    self.session_id = new_session["id"]
                    self.connected = True
                    self.on_status({
                        "connected": True,
                        "message": "EventSub WebSocket reconnected",
                        "session_id": self.session_id,
                        "login": self.identity.login if self.identity else "",
                    })
                    try:
                        await old_ws.close()
                    except Exception:
                        pass

                elif msg_type == "session_keepalive":
                    pass

                elif msg_type == "revocation":
                    self.connected = False
                    # Do not let the outer reconnect loop immediately create
                    # another session with a revoked subscription/token.
                    self.running = False
                    self.on_status({
                        "connected": False,
                        "message": "EventSub subscription revoked; Twitch must be reauthorized",
                        "raw": msg,
                    })
                    return

        finally:
            self.connected = False
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            if self.ws is ws:
                self.ws = None
            self.session_id = None
            self.on_status({"connected": False, "message": "EventSub WebSocket closed"})
