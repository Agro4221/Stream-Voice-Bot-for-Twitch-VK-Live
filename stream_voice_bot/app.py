from __future__ import annotations

import asyncio
import json
import platform
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import sounddevice as sd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .db import Database
from .models import QueueItem
from .stt import STTService
from .tts import AudioPlayer, PlayerSettings, SileroV5, TTSQueue
from .twitch import TwitchService
from .vkplay import VKPlayService
from .secrets import SecretStore


class QueueRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    username: str = "admin"
    source: str = "admin"
    profile: str = "normal"


class RepeatManyRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=50)
    profile: str = "normal"


class SettingsRequest(BaseModel):
    normal_speaker: str | None = None
    normal_volume: float | None = Field(default=None, ge=0, le=2.5)
    loud_speaker: str | None = None
    loud_volume: float | None = Field(default=None, ge=0, le=2.5)
    speed: float | None = Field(default=None, ge=0.5, le=1.5)
    max_chars: int | None = Field(default=None, ge=1, le=1000)
    output_device: int | None = None


class TwitchConfigRequest(BaseModel):
    client_id: str = ""
    client_secret: str = ""  # blank = keep saved secret
    redirect_uri: str = "http://localhost:8787/auth/twitch/callback"


class RewardRuleRequest(BaseModel):
    reward_id: str
    reward_title: str
    enabled: bool = True
    profile: str = "normal"
    auto_fulfill: bool = True
    user_input_required: bool = True
    prompt: str = "Введите текст для озвучки"


class VKPlayConfigRequest(BaseModel):
    channel_id: str = ""


class NormalizeRequest(BaseModel):
    text: str


class STTConfigRequest(BaseModel):
    model_name: str | None = None
    language: str | None = None
    input_device: int | None = None
    sample_rate: int | None = None
    chunk_seconds: float | None = Field(default=None, ge=1, le=10)
    overlap_seconds: float | None = Field(default=None, ge=0, le=3)
    beam_size: int | None = Field(default=None, ge=1, le=10)
    compute_type: str | None = None


def find_model(root: Path) -> Path | None:
    model_dir = root / "models"
    preferred = model_dir / "v5_ru.pt"
    legacy = root / "v5_ru.pt"
    try:
        if preferred.is_file() and preferred.stat().st_size > 1_000_000:
            return preferred.resolve()
        if legacy.is_file() and legacy.stat().st_size > 1_000_000:
            model_dir.mkdir(parents=True, exist_ok=True)
            if not preferred.exists() or preferred.stat().st_size != legacy.stat().st_size:
                shutil.copy2(legacy, preferred)
            return preferred.resolve()
    except OSError:
        pass
    return None


def create_app(root: Path) -> FastAPI:
    app = FastAPI(title="Stream Voice Bot", version="1.0.0")
    data_dir = root / "data"
    db = Database(data_dir / "stream_voice_bot.sqlite3")

    model_path = find_model(root) or (root / "v5_ru.pt")
    db.set_setting("model_path", str(model_path))

    normal_speaker = db.get_setting("normal_speaker", "xenia")
    normal_volume = float(db.get_setting("normal_volume", "1.0"))
    loud_speaker = db.get_setting("loud_speaker", normal_speaker)
    loud_volume = float(db.get_setting("loud_volume", "1.8"))
    speed = float(db.get_setting("speed", "1.0"))
    max_chars = int(db.get_setting("max_chars", "300"))
    output_device = int(db.get_setting("output_device")) if db.get_setting("output_device") else None

    profiles = {
        "normal": {"speaker": normal_speaker, "volume": normal_volume},
        "loud": {"speaker": loud_speaker, "volume": loud_volume},
    }

    player = AudioPlayer(PlayerSettings(
        device=output_device,
        volume=normal_volume,
        speed=speed,
        speaker=normal_speaker,
    ))
    model = SileroV5(model_path=model_path, device="cpu")

    active_profile = {"name": "normal"}

    queue = TTSQueue(
        model=model,
        player=player,
        speaker_getter=lambda: profiles[active_profile["name"]]["speaker"],
        volume_setter=lambda: profiles[active_profile["name"]]["volume"],
        history_db=db,
        max_chars_getter=lambda: int(db.get_setting("max_chars", "300")),
    )
    queue._profile_speaker_getter = lambda name: profiles[name]["speaker"]
    queue._profile_volume_getter = lambda name: profiles[name]["volume"]

    subtitle_lock = threading.Lock()
    subtitle_state = {
        "text": "",
        "timestamp": 0,
        "language": "",
    }

    secret_store = SecretStore()
    twitch_status = {
        "connected": False,
        "configured": bool(db.get_setting("twitch_client_id", "") and secret_store.get("twitch_client_secret")),
        "message": "Не подключён",
        "login": db.get_setting("twitch_login", ""),
    }

    vk_status = {
        "connected": False,
        "configured": bool(db.get_setting("vkplay_channel_id", "")),
        "message": "Не настроен",
    }

    async def schedule(coro):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            pass

    def on_subtitle(data: dict):
        with subtitle_lock:
            subtitle_state.update({
                "text": data.get("text", ""),
                "timestamp": data.get("timestamp", time.time()),
                "language": data.get("language", ""),
            })

    def on_twitch_status(data: dict):
        twitch_status.update(data)

    def on_twitch_chat(event: dict):
        text = (event.get("message", {}) or {}).get("text", "").strip()
        if not text:
            return
        username = event.get("chatter_user_name", "unknown")
        db.save_chat_message(
            platform="twitch",
            message_id=event.get("message_id"),
            broadcaster_user_id=event.get("broadcaster_user_id", ""),
            broadcaster_login=event.get("broadcaster_user_login", ""),
            user_id=event.get("chatter_user_id", ""),
            username=username,
            text=text,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            raw_json=json.dumps(event, ensure_ascii=False),
        )
        db.add_history(username, text, "twitch-chat", time.strftime("%Y-%m-%dT%H:%M:%S"), status="received", profile="normal")

    def on_vk_chat(event: dict):
        text = (event.get("text") or "").strip()
        if not text:
            return
        username = event.get("username", "unknown")
        db.save_chat_message(
            platform="vkplay", message_id=event.get("id"),
            broadcaster_user_id=db.get_setting("vkplay_channel_id", ""),
            broadcaster_login="", user_id="", username=username, text=text,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            raw_json=json.dumps(event, ensure_ascii=False),
        )
        db.add_history(username, text, "vkplay-chat", time.strftime("%Y-%m-%dT%H:%M:%S"), status="received", profile="normal")

    def on_vk_status(data: dict):
        vk_status.update(data)

    def on_redemption(event: dict):
        # Only process new/unfulfilled redemptions.
        if (event.get("status") or "").lower() not in {"", "unfulfilled"}:
            return
        reward = event.get("reward", {}) or {}
        reward_id = reward.get("id", "")
        rule = db.get_reward(reward_id)
        username = event.get("user_name", "unknown")
        user_input = (event.get("user_input") or "").strip()
        redemption_id = event.get("id", "")

        if not rule or not int(rule["enabled"]):
            # Intentionally leave unconfigured redemptions untouched.
            return
        if not user_input:
            # When input is configured as required, an empty event is invalid;
            # don't invent text and don't speak the redemption.
            if int(rule.get("user_input_required", 1)):
                asyncio.create_task(
                    twitch.update_redemption(reward_id, redemption_id, "CANCELED")
                )
                return
            user_input = f"{username} активировал награду «{reward.get('title', rule['reward_title'])}»"

        try:
            item = QueueItem(user_input, username, "twitch-channel-points")
            history_id = queue.enqueue(item, profile=rule["profile"])
            if int(rule["auto_fulfill"]):
                asyncio.create_task(
                    fulfill_after(history_id, reward_id, redemption_id)
                )
        except Exception:
            # The redemption remains pending in Twitch if enqueue failed.
            pass

    async def fulfill_after(history_id: int, reward_id: str, redemption_id: str):
        # Fulfill once the TTS item reaches a terminal state. We poll SQLite,
        # avoiding coupling Twitch's async client to the TTS worker thread.
        for _ in range(600):
            row = db.get_history(history_id)
            if row and row["status"] in {"finished", "stopped", "skipped", "audio_error", "error"}:
                status = "FULFILLED" if row["status"] == "finished" else "CANCELED"
                try:
                    await twitch.update_redemption(reward_id, redemption_id, status)
                except Exception:
                    pass
                return
            await asyncio.sleep(0.5)

    twitch = TwitchService(db, on_twitch_chat, on_redemption, on_twitch_status)
    vkplay = VKPlayService(db, on_vk_chat, on_vk_status)
    stt = STTService(db, on_subtitle, lambda data: setattr(app.state, "stt_status", data))

    # Completion callback is kept lightweight. Twitch fulfillment polls DB.
    app.state.db = db
    app.state.tts_model = model
    app.state.tts_queue = queue
    app.state.player = player
    app.state.profiles = profiles
    app.state.active_profile = active_profile
    app.state.twitch = twitch
    app.state.stt = stt
    app.state.twitch_status = twitch_status
    app.state.subtitle_state = subtitle_state

    @app.on_event("startup")
    async def startup():
        try:
            if twitch.configured() and await twitch.validate_token():
                await twitch.start()
        except Exception as e:
            twitch_status["connected"] = False
            twitch_status["message"] = f"Автоподключение Twitch: {type(e).__name__}: {e}"
        try:
            if vkplay.configured():
                await vkplay.start()
        except Exception as e:
            vk_status["connected"] = False
            vk_status["message"] = f"Автоподключение VK: {type(e).__name__}: {e}"

    @app.on_event("shutdown")
    async def shutdown():
        await twitch.stop()
        await vkplay.stop()
        stt.stop()
        queue.shutdown()

    def html_no_cache(path: Path):
        return HTMLResponse(
            path.read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )

    @app.get("/")
    async def index():
        return html_no_cache(root / "stream_voice_bot" / "web" / "index.html")

    @app.get("/subtitles")
    async def subtitles():
        return html_no_cache(root / "stream_voice_bot" / "web" / "subtitles.html")

    @app.get("/api/state")
    async def state():
        mp = find_model(root)
        model_exists = bool(mp and mp.is_file())
        with subtitle_lock:
            sub = dict(subtitle_state)
        twitch_scopes = []
        try:
            twitch_scopes = json.loads(db.get_setting("twitch_scopes", "[]") or "[]")
        except Exception:
            pass
        stt_status = getattr(app.state, "stt_status", {})
        return {
            "app": {"name": "Stream Voice Bot", "version": "1.0.0"},
            "model": {
                "path": str(model_path),
                "exists": model_exists,
                "loaded": model.model is not None,
            },
            "queue": queue.state(),
            "profiles": profiles,
            "active_profile": active_profile["name"],
            "twitch": {
                **twitch_status,
                "scopes": twitch_scopes,
                "client_configured": twitch.configured(),
                "authorized": bool(twitch.access_token()),
                "user": {
                    "login": db.get_setting("twitch_login", ""),
                    "display_name": db.get_setting("twitch_display_name", ""),
                    "user_id": db.get_setting("twitch_user_id", ""),
                },
            },
            "vkplay": {
                **vk_status,
                "channel_id": db.get_setting("vkplay_channel_id", ""),
                "bridge_available": (root / "stream_voice_bot" / "vk_bridge" / "bridge.js").exists(),
                "service_key_saved": secret_store.has("vk_service_key"),
                "secure_key_saved": secret_store.has("vk_secure_key"),
            },
            "stt": {**stt.state(), **stt_status},
            "subtitle": sub,
            "settings": {
                "speed": float(db.get_setting("speed", "1.0")),
                "max_chars": int(db.get_setting("max_chars", "300")),
                "output_device": int(db.get_setting("output_device")) if db.get_setting("output_device") else None,
            },
            "audio": {
                "last_error": player.last_error,
                "device": player.last_device,
                "sample_rate": player.last_sample_rate,
                "device_default_sample_rate": player.last_device_default_rate,
                "recommended_sample_rate": 48000,
            },
            "system": {
                "platform": platform.platform(),
                "ffmpeg": shutil.which("ffmpeg"),
            },
        }

    @app.get("/api/history")
    async def history(limit: int = 100):
        return db.history(limit)

    @app.get("/api/audio/devices")
    async def audio_devices():
        devices = []
        for i, d in enumerate(sd.query_devices()):
            devices.append({
                "id": i,
                "name": d["name"],
                "input_channels": int(d["max_input_channels"]),
                "output_channels": int(d["max_output_channels"]),
                "default_samplerate": float(d["default_samplerate"]),
            })
        return devices

    @app.post("/api/tts/normalize")
    async def tts_normalize(req: NormalizeRequest):
        return {"ok": True, "original": req.text, "normalized": model.normalize_preview(req.text)}

    @app.post("/api/audio/test")
    async def audio_test():
        result = player.test_tone()
        if result != "finished":
            raise HTTPException(status_code=500, detail=player.last_error or "Audio test failed")
        return {"ok": True, "device": player.last_device, "sample_rate": player.last_sample_rate}

    @app.post("/api/settings")
    async def update_settings(req: SettingsRequest):
        supported = {"aidar", "baya", "kseniya", "xenia", "eugene"}
        if req.normal_speaker is not None:
            if req.normal_speaker not in supported:
                raise HTTPException(400, "Unsupported normal speaker")
            db.set_setting("normal_speaker", req.normal_speaker)
            profiles["normal"]["speaker"] = req.normal_speaker
        if req.normal_volume is not None:
            db.set_setting("normal_volume", str(req.normal_volume))
            profiles["normal"]["volume"] = req.normal_volume
        if req.loud_speaker is not None:
            if req.loud_speaker not in supported:
                raise HTTPException(400, "Unsupported loud speaker")
            db.set_setting("loud_speaker", req.loud_speaker)
            profiles["loud"]["speaker"] = req.loud_speaker
        if req.loud_volume is not None:
            db.set_setting("loud_volume", str(req.loud_volume))
            profiles["loud"]["volume"] = req.loud_volume
        if req.speed is not None:
            db.set_setting("speed", str(req.speed))
            player.settings.speed = req.speed
        if req.max_chars is not None:
            db.set_setting("max_chars", str(req.max_chars))
        if req.output_device is not None:
            try:
                d = sd.query_devices(req.output_device)
            except Exception as e:
                raise HTTPException(400, f"Invalid audio device: {e}")
            if int(d["max_output_channels"]) <= 0:
                raise HTTPException(400, "Selected device has no output")
            db.set_setting("output_device", str(req.output_device))
            player.settings.device = req.output_device
        return {"ok": True, "profiles": profiles}

    @app.post("/api/profile/{name}")
    async def set_profile(name: str):
        if name not in profiles:
            raise HTTPException(400, "Unknown profile")
        active_profile["name"] = name
        return {"ok": True, "profile": name}

    @app.post("/api/queue")
    async def enqueue(req: QueueRequest):
        profile = req.profile if req.profile in profiles else "normal"
        try:
            item = QueueItem(req.text.strip(), req.username.strip() or "admin", req.source)
            hid = queue.enqueue(item, profile=profile)
            return {"ok": True, "history_id": hid, "profile": profile}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/queue/pause")
    async def pause():
        queue.pause()
        return {"ok": True}

    @app.post("/api/queue/resume")
    async def resume():
        queue.resume()
        return {"ok": True}

    @app.post("/api/queue/stop")
    async def stop():
        queue.stop()
        return {"ok": True}

    @app.post("/api/queue/skip")
    async def skip():
        queue.skip()
        return {"ok": True}

    @app.post("/api/queue/clear")
    async def clear():
        queue.clear()
        return {"ok": True}

    @app.post("/api/repeat/{history_id}")
    async def repeat_one(history_id: int, profile: str = "normal"):
        row = db.get_history(history_id)
        if not row:
            raise HTTPException(404, "History item not found")
        item = QueueItem(row["text"], row["username"], "repeat", repeat_of=history_id)
        hid = queue.enqueue(item, profile=profile if profile in profiles else "normal")
        return {"ok": True, "history_id": hid}

    @app.post("/api/repeat-many")
    async def repeat_many(req: RepeatManyRequest):
        profile = req.profile if req.profile in profiles else "normal"
        new_ids = []
        for history_id in req.ids:
            row = db.get_history(history_id)
            if not row:
                continue
            item = QueueItem(row["text"], row["username"], "repeat-many", repeat_of=history_id)
            new_ids.append(queue.enqueue(item, profile=profile))
        if not new_ids:
            raise HTTPException(404, "No valid history items selected")
        return {"ok": True, "history_ids": new_ids}

    @app.post("/api/tts/test")
    async def test_tts(profile: str = "normal"):
        mp = find_model(root)
        if mp is None:
            raise HTTPException(400, "Silero model not found")
        if model.model_path.resolve() != mp.resolve() or model.model is None:
            model.model_path = mp
            model.model = None
        item = QueueItem(
            "Это тестовая озвучка стрим-бота. AIMP не используется.",
            "BOT",
            "admin-test",
        )
        hid = queue.enqueue(item, profile=profile if profile in profiles else "normal")
        return {"ok": True, "queued": True, "history_id": hid, "profile": profile}

    # ---- Twitch OAuth + EventSub ----
    @app.post("/api/twitch/config")
    async def twitch_config(req: TwitchConfigRequest):
        if not req.client_id.strip():
            raise HTTPException(400, "Client ID is required")

        saved_secret = secret_store.get("twitch_client_secret")
        new_secret = req.client_secret.strip()

        if new_secret:
            try:
                secret_store.set("twitch_client_secret", new_secret)
            except Exception as e:
                raise HTTPException(
                    500,
                    f"Не удалось сохранить Twitch Client Secret в хранилище Windows: {e}",
                ) from e
            saved_secret = new_secret

        if not saved_secret:
            raise HTTPException(
                400,
                "Client Secret не сохранён. Введи его один раз и нажми «Сохранить Twitch».",
            )

        db.set_setting("twitch_client_id", req.client_id.strip())
        db.set_setting("twitch_redirect_uri", req.redirect_uri.strip())
        twitch_status["configured"] = True
        return {
            "ok": True,
            "secret_saved": True,
            "message": "Client Secret сохранён. Повторно вводить его не нужно.",
        }

    @app.get("/api/twitch/config")
    async def twitch_config_state():
        return {
            "client_id": db.get_setting("twitch_client_id", ""),
            "redirect_uri": db.get_setting("twitch_redirect_uri", "http://localhost:8787/auth/twitch/callback"),
            "secret_saved": bool(secret_store.get("twitch_client_secret")),
            "authorized": bool(twitch.access_token()),
        }

    @app.post("/api/twitch/credentials/test")
    async def twitch_credentials_test():
        saved = secret_store.get("twitch_client_secret")
        if not saved:
            return {"ok": False, "secret_saved": False}
        return {
            "ok": True,
            "secret_saved": True,
            "message": "Twitch Client Secret доступен из защищённого локального хранилища.",
        }

    @app.get("/auth/twitch/start")
    async def twitch_start():
        if not twitch.configured():
            raise HTTPException(400, "Configure Twitch Client ID/Secret first")
        return RedirectResponse(twitch.authorization_url())

    @app.get("/auth/twitch/callback", response_class=HTMLResponse)
    async def twitch_callback(code: str | None = None, state: str | None = None, error: str | None = None):
        if error:
            return HTMLResponse(f"<h2>Twitch authorization failed</h2><p>{error}</p>", status_code=400)
        if not code or not state:
            return HTMLResponse("<h2>Missing Twitch OAuth code/state.</h2>", status_code=400)
        try:
            await twitch.exchange_code(code, state)
            await twitch.start()
            return HTMLResponse(
                "<h2>Готово.</h2><p>Twitch подключён. Можно закрыть это окно и вернуться в админку.</p>"
            )
        except Exception as e:
            return HTMLResponse(
                f"<h2>Ошибка Twitch</h2><pre>{str(e)}</pre>",
                status_code=500,
            )

    @app.post("/api/twitch/start")
    async def twitch_connect():
        try:
            await twitch.start()
            return {"ok": True}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/twitch/stop")
    async def twitch_disconnect():
        await twitch.stop()
        return {"ok": True}

    @app.get("/api/twitch/rewards")
    async def twitch_rewards():
        if not twitch.access_token():
            raise HTTPException(400, "Twitch is not authorized")
        try:
            data = await twitch.get_custom_rewards()
            return data
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.get("/api/twitch/rules")
    async def twitch_rules():
        return db.rewards()

    @app.post("/api/twitch/rules")
    async def twitch_rule(req: RewardRuleRequest):
        if req.profile not in profiles:
            raise HTTPException(400, "Unknown profile")
        if req.user_input_required and not req.prompt.strip():
            raise HTTPException(400, "Prompt is required when text input is enabled")
        if len(req.prompt) > 200:
            raise HTTPException(400, "Twitch prompt is limited to 200 characters")

        try:
            await twitch.update_custom_reward(
                req.reward_id,
                title=req.reward_title,
                prompt=req.prompt.strip(),
                is_enabled=req.enabled,
                is_user_input_required=req.user_input_required,
                should_redemptions_skip_request_queue=False,
            )
        except Exception as e:
            raise HTTPException(500, f"Twitch reward update failed: {e}") from e

        db.save_reward(
            req.reward_id,
            req.reward_title,
            req.enabled,
            req.profile,
            req.auto_fulfill,
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            user_input_required=req.user_input_required,
            prompt=req.prompt.strip(),
        )
        return {"ok": True}

    @app.delete("/api/twitch/rules/{reward_id}")
    async def twitch_rule_delete(reward_id: str):
        # Use raw SQL deliberately only for admin UI deletion.
        with db.lock, db._connect() as conn:
            conn.execute("DELETE FROM twitch_rewards WHERE reward_id=?", (reward_id,))
        return {"ok": True}

    # ---- VK Video Live ----
    @app.post("/api/vkplay/config")
    async def vkplay_config(req: VKPlayConfigRequest):
        db.set_setting("vkplay_channel_id", req.channel_id.strip())
        vk_status["configured"] = bool(req.channel_id.strip())
        return {"ok": True, "channel_id": req.channel_id.strip()}

    @app.get("/api/vkplay/config")
    async def vkplay_config_state():
        return {
            "channel_id": db.get_setting("vkplay_channel_id", ""),
            "service_key_saved": secret_store.has("vk_service_key"),
            "secure_key_saved": secret_store.has("vk_secure_key"),
        }

    @app.post("/api/vkplay/credentials")
    async def vkplay_credentials(req: dict):
        service_key = str(req.get("service_key", "")).strip()
        secure_key = str(req.get("secure_key", "")).strip()
        try:
            if service_key:
                secret_store.set("vk_service_key", service_key)
            if secure_key:
                secret_store.set("vk_secure_key", secure_key)
        except Exception as e:
            raise HTTPException(500, f"Не удалось сохранить VK credentials: {e}") from e

        return {
            "ok": True,
            "service_key_saved": secret_store.has("vk_service_key"),
            "secure_key_saved": secret_store.has("vk_secure_key"),
        }

    @app.post("/api/vkplay/start")
    async def vkplay_start():
        try:
            await vkplay.start()
            return {"ok": True}
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/vkplay/stop")
    async def vkplay_stop():
        await vkplay.stop()
        return {"ok": True}

    # ---- STT ----
    @app.post("/api/stt/config")
    async def stt_config(req: STTConfigRequest):
        kwargs = req.model_dump(exclude_none=True)
        stt.save_config(**kwargs)
        return {"ok": True, "state": stt.state()}

    @app.post("/api/stt/start")
    async def stt_start():
        try:
            stt.start()
            return {"ok": True, "state": stt.state()}
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.post("/api/stt/stop")
    async def stt_stop():
        stt.stop()
        return {"ok": True}

    return app
