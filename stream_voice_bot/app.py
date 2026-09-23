from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import re
import platform
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import sounddevice as sd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .db import Database
from .log_buffer import clear as clear_runtime_logs
from .log_buffer import get_logs, install as install_log_buffer
from .models import QueueItem, utc_now
from .stt import STTService
from .tts import AudioPlayer, PlayerSettings, SileroV5, TTSQueue
from .translator import TranslationService
from .twitch import TwitchService
from .vkplay import VKPlayService, parse_vk_reward_announcement
from .secrets import SecretStore


log = logging.getLogger("stream_voice_bot.app")


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
    normal_volume: float | None = Field(default=None, ge=-12, le=12)
    loud_speaker: str | None = None
    loud_volume: float | None = Field(default=None, ge=-12, le=12)
    speed: float | None = Field(default=None, ge=0.5, le=1.5)
    max_chars: int | None = Field(default=None, ge=1, le=1000)
    output_device: int | None = None


class TwitchConfigRequest(BaseModel):
    client_id: str = ""
    client_secret: str = ""  # blank = keep saved secret
    redirect_uri: str = "http://localhost:8787/auth/twitch/callback"


# Kept for API compatibility with older admin pages. The current UI no longer
# requires per-reward rules; Twitch TTS uses the fixed "Озвучить сообщение" reward.
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
    device_mode: str | None = None


def _is_usable_model(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 1_000_000:
            return False
        with path.open("rb") as fh:
            return bool(fh.read(16))
    except OSError:
        return False


def find_model(root: Path) -> Path | None:
    model_dir = root / "models"
    preferred = model_dir / "v5_ru.pt"
    legacy = root / "v5_ru.pt"
    model_dir.mkdir(parents=True, exist_ok=True)

    if _is_usable_model(preferred):
        return preferred.resolve()
    if _is_usable_model(legacy):
        try:
            shutil.copy2(legacy, preferred)
        except OSError:
            return legacy.resolve()
        return preferred.resolve() if _is_usable_model(preferred) else legacy.resolve()
    return None


def install_silero_model(root: Path, force: bool = False) -> Path:
    model_dir = root / "models"
    model_dir.mkdir(parents=True, exist_ok=True)
    target = model_dir / "v5_ru.pt"
    if not force and _is_usable_model(target):
        return target.resolve()

    partial = model_dir / "v5_ru.pt.part"
    url = "https://models.silero.ai/models/tts/ru/v5_ru.pt"
    try:
        if partial.exists():
            partial.unlink()
    except OSError:
        pass
    urllib.request.urlretrieve(url, partial)
    if not _is_usable_model(partial):
        try:
            partial.unlink()
        except OSError:
            pass
        raise RuntimeError("Silero model download failed or returned an invalid file")
    partial.replace(target)
    return target.resolve()


def _read_app_version(root: Path) -> str:
    try:
        value = (root / "VERSION").read_text(encoding="utf-8").strip()
        return value or "0.0.0-dev"
    except OSError:
        return "0.0.0-dev"


def create_app(root: Path, data_root: Path | None = None) -> FastAPI:
    """Build the app from bundled resources while keeping writable data external.

    In a PyInstaller onedir bundle, bundled resources live under `_internal`,
    while user data should remain beside the EXE so it survives updates.
    Source/developer runs keep the historical single-root behavior.
    """
    app_version = _read_app_version(root)
    app = FastAPI(title="Stream Voice Bot", version=app_version)
    install_log_buffer()
    log.info("Admin backend initialized (version=%s)", app_version)
    data_dir = (data_root or root) / "data"
    db = Database(data_dir / "stream_voice_bot.sqlite3")

    model_path = find_model(root) or (root / "models" / "v5_ru.pt")
    db.set_setting("model_path", "models/v5_ru.pt")

    # Keep existing installs on a known live-caption window. Older releases
    # used experimental 2.0/2.5s + 0.25s settings; the 1.1.11 candidate briefly
    # moved to 4.0/0.5, which is too slow for live captions. Migrate those
    # known windows to the restored 2.5/0.25 profile once.
    if db.get_setting("stt_stable_window_migrated") != "1":
        saved_chunk = db.get_setting("stt_chunk_seconds", "")
        saved_overlap = db.get_setting("stt_overlap_seconds", "")
        if saved_chunk in {"2", "2.0", "2.5", "2.50"} and saved_overlap in {"0.25", ".25"}:
            db.set_setting("stt_chunk_seconds", "2.5")
            db.set_setting("stt_overlap_seconds", "0.25")
        db.set_setting("stt_stable_window_migrated", "1")

    # Existing 1.1.11 candidate installs already have the stable-window marker
    # set and therefore need a separate one-time migration from 4.0/0.5.
    if db.get_setting("stt_live_latency_v2_migrated") != "1":
        saved_chunk = db.get_setting("stt_chunk_seconds", "")
        saved_overlap = db.get_setting("stt_overlap_seconds", "")
        if saved_chunk in {"4", "4.0", "4.00"} and saved_overlap in {"0.5", ".5", "0.50"}:
            db.set_setting("stt_chunk_seconds", "2.5")
            db.set_setting("stt_overlap_seconds", "0.25")
        db.set_setting("stt_live_latency_v2_migrated", "1")

    db.prune_history(max_rows=50000)

    normal_speaker = db.get_setting("normal_speaker", "xenia")
    normal_volume = float(db.get_setting("normal_volume", "0.0"))
    loud_speaker = db.get_setting("loud_speaker", normal_speaker)
    loud_volume = float(db.get_setting("loud_volume", "6.0"))
    # Migrate old 0..2.5 multiplier values once (legacy UI).
    if 0 <= normal_volume <= 2.5 and db.get_setting("volume_format_migrated") != "1":
        normal_volume = round(20.0 * math.log10(max(normal_volume, 1e-6)), 2) if normal_volume > 0 else -12.0
    if 0 <= loud_volume <= 2.5 and db.get_setting("volume_format_migrated") != "1":
        loud_volume = round(20.0 * math.log10(max(loud_volume, 1e-6)), 2) if loud_volume > 0 else -12.0
    if db.get_setting("volume_format_migrated") != "1":
        db.set_setting("normal_volume", str(normal_volume))
        db.set_setting("loud_volume", str(loud_volume))
        db.set_setting("volume_format_migrated", "1")
    speed = float(db.get_setting("speed", "1.0"))
    max_chars = int(db.get_setting("max_chars", "300"))
    output_device = int(db.get_setting("output_device")) if db.get_setting("output_device") else None

    profiles = {
        "normal": {"speaker": normal_speaker, "volume_db": normal_volume},
        "loud": {"speaker": loud_speaker, "volume_db": loud_volume},
    }

    player = AudioPlayer(PlayerSettings(
        device=output_device,
        volume_db=normal_volume,
        speed=speed,
        speaker=normal_speaker,
    ))
    model = SileroV5(model_path=model_path, device="cpu")

    active_profile = {"name": "normal"}

    queue = TTSQueue(
        model=model,
        player=player,
        speaker_getter=lambda: profiles[active_profile["name"]]["speaker"],
        volume_setter=lambda: profiles[active_profile["name"]]["volume_db"],
        history_db=db,
        max_chars_getter=lambda: int(db.get_setting("max_chars", "300")),
    )
    queue._profile_speaker_getter = lambda name: profiles[name]["speaker"]
    queue._profile_volume_getter = lambda name: profiles[name]["volume_db"]

    subtitle_lock = threading.Lock()
    default_subtitle_tracks = [
        {"id": "ru", "name": "Русский", "language": "ru", "enabled": True, "mode": "source"},
        {"id": "en", "name": "English", "language": "en", "enabled": False, "mode": "translate"},
    ]
    try:
        subtitle_tracks = json.loads(db.get_setting("subtitle_tracks", json.dumps(default_subtitle_tracks, ensure_ascii=False)))
        if not isinstance(subtitle_tracks, list) or not subtitle_tracks:
            subtitle_tracks = default_subtitle_tracks
        for track in subtitle_tracks:
            if track.get("mode") == "whisper-translate":
                track["mode"] = "translate"
    except Exception:
        subtitle_tracks = default_subtitle_tracks
    _SUBTITLE_CREDIT_RE = re.compile(
        r"(?:\bsubtitles?\s+(?:made|created|provided)\s+by\b|"
        r"\b(?:субтитры|субтитров)\s+(?:сделаны|сделано|созданы|создано|предоставлены)\b|"
        r"\bdima\s*torzok\b)",
        re.IGNORECASE,
    )

    def _is_bad_subtitle_text(value: str) -> bool:
        return bool(_SUBTITLE_CREDIT_RE.search(" ".join(str(value or "").split())))

    subtitle_state = {
        str(t.get("id", "ru")): {"text": "", "timestamp": 0, "language": t.get("language", "ru")}
        for t in subtitle_tracks
    }

    translation_status = {"message": "Перевод субтитров не настроен"}
    model_status = {"installing": False, "message": ""}
    translator = TranslationService(lambda data: translation_status.update(data))

    secret_store = SecretStore()
    twitch_status = {
        "connected": False,
        "configured": bool(db.get_setting("twitch_client_id", "")),
        "message": "Не подключён",
        "login": db.get_setting("twitch_login", ""),
    }

    vk_status = {
        "connected": False,
        "configured": bool(db.get_setting("vkplay_channel_id", "")),
        "message": "Не настроен",
    }

    device_task: asyncio.Task | None = None
    vk_recent_chat: dict[tuple[str, str], float] = {}
    vk_recent_reward: dict[tuple[str, str], float] = {}
    VK_REWARD_DEDUPE_SECONDS = 30.0

    async def schedule(coro):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            pass

    def on_subtitle(data: dict):
        text_value = str(data.get("text") or "").strip()
        if not text_value or _is_bad_subtitle_text(text_value):
            log.warning("Blocked subtitle hallucination/credit text: %r", text_value)
            return

        explicit_track = str(data.get("track_id") or "").strip().lower()
        is_translated = data.get("source") is False
        with subtitle_lock:
            if is_translated and explicit_track and explicit_track in subtitle_state:
                subtitle_state[explicit_track].update({
                    "text": data.get("text", ""),
                    "timestamp": data.get("timestamp", time.time()),
                    "language": data.get("language", explicit_track),
                })
                return

            # Original STT text always goes to the enabled source tracks.
            # Whisper's detected language is metadata and must not control routing.
            source_tracks = [
                t for t in subtitle_tracks
                if t.get("enabled", True) and t.get("mode", "source") == "source"
            ]
            for t in source_tracks or [{"id": "ru", "language": data.get("language", "ru")}]:
                tid = str(t.get("id", "ru"))
                subtitle_state.setdefault(tid, {"text": "", "timestamp": 0, "language": ""})
                subtitle_state[tid].update({
                    "text": data.get("text", ""),
                    "timestamp": data.get("timestamp", time.time()),
                    "language": data.get("language", t.get("language", "ru")),
                })

    def on_twitch_status(data: dict):
        twitch_status.update(data)
        message = str(data.get("message") or "").strip()
        if message:
            log.info("Twitch: %s", message)

    def _vk_dedupe_key(username: str, text: str) -> tuple[str, str]:
        return (
            " ".join((username or "").casefold().split()),
            " ".join((text or "").split()),
        )

    def _vk_prune_dedupe(now: float):
        cutoff = now - VK_REWARD_DEDUPE_SECONDS
        for bucket in (vk_recent_chat, vk_recent_reward):
            for key, seen_at in list(bucket.items()):
                if seen_at < cutoff:
                    bucket.pop(key, None)

    def on_twitch_chat(event: dict):
        # Normal Twitch chat is intentionally ignored here. Channel Points
        # redemptions arrive through EventSub redemption events and are handled
        # by on_redemption(); otherwise every ordinary chat message would be
        # spoken as if it were a reward.
        return

    def on_vk_chat(event: dict):
        # The readonly VK client receives ordinary viewer messages too.
        # Only ChatBot system messages are allowed to reach the reward parser.
        if not bool(event.get("is_chatbot")):
            return

        raw_text = (event.get("text") or "").strip()
        if not raw_text:
            return

        # VK exposes the reward as a system chat announcement such as:
        # "ChatBot: User получает награду: Озвучить сообщение за 2: текст".
        # Ordinary viewer messages are never TTS input.
        reward = parse_vk_reward_announcement(raw_text)
        if not reward:
            return

        message_id = event.get("id")
        if message_id and not db.claim_event("vk:" + str(message_id)):
            return

        username = reward["username"]
        text = reward["text"]
        created_at = utc_now()
        db.save_chat_message(
            platform="vkplay",
            message_id=message_id,
            broadcaster_user_id=db.get_setting("vkplay_channel_id", ""),
            broadcaster_login="",
            user_id="",
            username=username,
            text=text,
            created_at=created_at,
            raw_json=json.dumps(event, ensure_ascii=False),
        )
        try:
            queue.enqueue(
                QueueItem(text, username, "vkplay-reward", created_at=created_at),
                profile="normal",
            )
            vk_status["last_event"] = f"VK награда: {username}"
            vk_status["message"] = "Награда принята в очередь озвучки."
            log.info("VK reward queued: user=%s", username)
        except Exception as e:
            db.add_history(
                username, text, "vkplay-reward", created_at,
                status="received", profile="normal",
            )
            log.exception("VK reward could not be queued: %s", e)

    def on_vk_status(data: dict):
        vk_status.update(data)
        message = str(data.get("message") or "").strip()
        if message:
            log.info("VK: %s", message)

    def on_redemption(event: dict):
        # Twitch Channel Points handling is deliberately simple: one reward
        # named "Озвучить сообщение" feeds its viewer-entered text into TTS.
        if (event.get("status") or "").lower() not in {"", "unfulfilled"}:
            return

        redemption_id = event.get("id", "")
        eventsub_id = event.get("_eventsub_message_id")
        reward = event.get("reward", {}) or {}
        reward_id = reward.get("id", "")
        reward_title = str(reward.get("title") or "").strip()
        username = event.get("user_name", "unknown")
        user_input = (event.get("user_input") or "").strip()

        twitch_status["last_event"] = (
            f"Channel Points: {username} → {reward_title or reward_id or 'неизвестная награда'}"
        )

        if reward_title.casefold() != "озвучить сообщение":
            twitch_status["message"] = (
                f"Награда «{reward_title or reward_id}» пропущена. "
                "Озвучивается только «Озвучить сообщение»."
            )
            log.info(
                "Twitch redemption ignored: reward is not the voice reward; "
                "user=%s reward=%s (%s)",
                username, reward_title, reward_id,
            )
            return

        dedupe_id = eventsub_id or ("redemption:" + str(redemption_id) if redemption_id else "")
        if dedupe_id and not db.claim_event("twitch:" + str(dedupe_id)):
            return

        if not user_input:
            twitch_status["message"] = (
                "Награда «Озвучить сообщение» получена без текста — отменяю."
            )
            log.info(
                "Twitch redemption canceled: missing viewer text; user=%s reward=%s",
                username, reward_title,
            )
            asyncio.create_task(
                twitch.update_redemption(reward_id, redemption_id, "CANCELED")
            )
            return

        try:
            item = QueueItem(user_input, username, "twitch-channel-points")
            history_id = queue.enqueue(item, profile="normal")
            twitch_status["message"] = (
                f"Channel Points: {username} → «Озвучить сообщение» добавлено в очередь."
            )
            asyncio.create_task(
                fulfill_after(history_id, reward_id, redemption_id)
            )
        except Exception as e:
            history_id = db.add_history(
                username,
                user_input,
                "twitch-channel-points",
                time.strftime("%Y-%m-%dT%H:%M:%S"),
                status="error",
                profile="normal",
            )
            log.exception("Twitch redemption could not be queued: %s", e)
            asyncio.create_task(
                fulfill_after(history_id, reward_id, redemption_id)
            )

    async def fulfill_after(history_id: int, reward_id: str, redemption_id: str):
        # Fulfill once the TTS item reaches a terminal state. We poll SQLite,
        # avoiding coupling Twitch's async client to the TTS worker thread.
        for _ in range(600):
            row = db.get_history(history_id)
            if row and row["status"] in {"finished", "stopped", "skipped", "cleared", "audio_error", "error"}:
                status = "FULFILLED" if row["status"] == "finished" else "CANCELED"
                for attempt in range(3):
                    try:
                        await twitch.update_redemption(reward_id, redemption_id, status)
                        return
                    except httpx.HTTPStatusError as e:
                        if e.response is not None and e.response.status_code == 403:
                            log.warning(
                                "Twitch redemption was spoken, but auto-fulfill is forbidden "
                                "for this reward. Twitch only allows the app that created the "
                                "reward to update its redemption status."
                            )
                            return
                        if attempt == 2:
                            log.exception(
                                "Twitch redemption update failed after retries: %s",
                                e,
                            )
                        else:
                            await asyncio.sleep(1.5 * (attempt + 1))
                    except Exception as e:
                        if attempt == 2:
                            log.exception(
                                "Twitch redemption update failed after retries: %s",
                                e,
                            )
                        else:
                            await asyncio.sleep(1.5 * (attempt + 1))
                return
            await asyncio.sleep(0.5)

    twitch = TwitchService(db, on_twitch_chat, on_redemption, on_twitch_status)
    vkplay = VKPlayService(db, on_vk_chat, on_vk_status)
    stt = STTService(db, on_subtitle, lambda data: setattr(app.state, "stt_status", data), lambda: subtitle_tracks, translator=translator)

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
    app.state.subtitle_tracks = subtitle_tracks

    @app.on_event("startup")
    async def startup():
        log.info("Bot startup")
        try:
            if twitch.configured() and await twitch.validate_token():
                await twitch.start()
        except Exception as e:
            twitch_status["connected"] = False
            twitch_status["message"] = f"Автоподключение Twitch: {type(e).__name__}: {e}"
            log.exception("Twitch auto-connect failed")
        try:
            if vkplay.configured():
                await vkplay.start()
        except Exception as e:
            vk_status["connected"] = False
            vk_status["message"] = f"Автоподключение VK: {type(e).__name__}: {e}"
            log.exception("VK auto-connect failed")

    @app.on_event("shutdown")
    async def shutdown():
        log.info("Bot shutdown requested")
        nonlocal device_task
        if device_task and not device_task.done():
            device_task.cancel()
            try:
                await device_task
            except asyncio.CancelledError:
                pass
        device_task = None
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

    @app.get("/subtitles/{track_id}")
    async def subtitles_track(track_id: str):
        track_id = track_id.strip().lower()
        if not track_id or not re.fullmatch(r"[a-z0-9_-]{1,32}", track_id):
            raise HTTPException(400, "Invalid subtitle track id")
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
            "app": {"name": "Stream Voice Bot", "version": app_version},
            "model": {
                "path": "models/v5_ru.pt",
                "exists": model_exists,
                "loaded": model.model is not None,
                "installing": bool(model_status.get("installing")),
                "message": model_status.get("message", ""),
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
            # The service state is authoritative. The callback snapshot may
            # contain fields from an earlier status event (for example a stale
            # CUDA device after a failed reload), so never let it overwrite the
            # live STT state.
            "stt": {**stt_status, **stt.state()},
            "subtitle": sub,
            "subtitle_tracks": subtitle_tracks,
            "translation": translation_status,
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

    @app.get("/api/logs")
    async def runtime_logs(limit: int = 250):
        return {"logs": get_logs(limit)}

    @app.post("/api/logs/clear")
    async def runtime_logs_clear():
        clear_runtime_logs()
        return {"ok": True}

    @app.get("/api/history")
    async def history(limit: int = 100):
        return db.history(limit)

    @app.post("/api/shutdown")
    async def shutdown():
        server = getattr(app.state, "server", None)
        if server is None:
            raise HTTPException(503, "Сервер запущен не через штатный launcher")

        # Let the HTTP response reach the browser before asking Uvicorn to
        # shut down. Setting should_exit inline can race the response and make
        # the browser report a misleading "Failed to fetch".
        async def stop_server_later():
            # Give the browser enough time to receive and process the 200 OK
            # before Uvicorn begins its graceful shutdown.
            await asyncio.sleep(2.0)
            server.should_exit = True

        asyncio.create_task(stop_server_later(), name="shutdown-server-later")
        return {"ok": True, "message": "Бот завершает работу…"}

    @app.delete("/api/history")
    async def clear_history():
        removed = db.clear_history()
        return {"ok": True, "removed": removed}

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
    async def audio_test(profile: str = "normal"):
        if profile not in profiles:
            raise HTTPException(400, "Unknown profile")
        result = player.test_tone(volume_db=profiles[profile]["volume_db"])
        if result != "finished":
            raise HTTPException(status_code=500, detail=player.last_error or "Audio test failed")
        return {"ok": True, "device": player.last_device, "sample_rate": player.last_sample_rate}

    @app.post("/api/model/install")
    async def model_install(force: bool = False):
        nonlocal model_path
        try:
            model_status["installing"] = True
            model_status["message"] = "Скачивание/проверка модели…"
            target = await asyncio.to_thread(install_silero_model, root, force)
            model_path = target
            with model.lock:
                model.model_path = target
                model.model = None
            db.set_setting("model_path", "models/v5_ru.pt")
            model_status["installing"] = False
            model_status["message"] = "Модель Silero готова ✓"
            return {"ok": True, "path": "models/v5_ru.pt"}
        except Exception as e:
            model_status["installing"] = False
            model_status["message"] = f"Ошибка: {type(e).__name__}: {e}"
            raise HTTPException(500, f"Silero model setup failed: {type(e).__name__}: {e}") from e

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
            profiles["normal"]["volume_db"] = req.normal_volume
            player.settings.volume_db = req.normal_volume
        if req.loud_speaker is not None:
            if req.loud_speaker not in supported:
                raise HTTPException(400, "Unsupported loud speaker")
            db.set_setting("loud_speaker", req.loud_speaker)
            profiles["loud"]["speaker"] = req.loud_speaker
        if req.loud_volume is not None:
            db.set_setting("loud_volume", str(req.loud_volume))
            profiles["loud"]["volume_db"] = req.loud_volume
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
        cleared = queue.clear()
        return {"ok": True, "cleared": cleared}

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

        db.set_setting("twitch_client_id", req.client_id.strip())
        db.set_setting("twitch_redirect_uri", req.redirect_uri.strip())
        twitch_status["configured"] = bool(req.client_id.strip())
        return {
            "ok": True,
            "secret_saved": bool(saved_secret),
            "message": "Twitch Client ID сохранён. Для нового подключения Client Secret больше не нужен — используем Device Code Flow.",
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

    @app.post("/api/twitch/device/start")
    async def twitch_device_start():
        nonlocal device_task
        try:
            payload = await twitch.start_device_flow()
            if device_task and not device_task.done():
                device_task.cancel()
            async def run_device_flow():
                try:
                    await twitch.finish_device_flow(
                        payload.get("device_code", ""),
                        int(payload.get("interval", 5)),
                        int(payload.get("expires_in", 1800)),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    twitch_status["connected"] = False
                    twitch_status["message"] = f"Device Flow: {type(e).__name__}: {e}"
                    twitch_status["device_pending"] = False

            device_task = asyncio.create_task(run_device_flow(), name="twitch-device-flow")
            return payload
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/api/twitch/device/status")
    async def twitch_device_status():
        return {
            "ok": True,
            "pending": bool(device_task and not device_task.done()),
            "authorized": bool(twitch.access_token()),
            "connected": bool(twitch.connected),
            "message": twitch_status.get("message", ""),
        }

    @app.get("/auth/twitch/start")
    async def twitch_start():
        if not twitch.configured():
            raise HTTPException(400, "Configure Twitch Client ID/Secret first")
        return RedirectResponse(twitch.authorization_url())

    @app.get("/auth/twitch/callback", response_class=HTMLResponse)
    async def twitch_callback(code: str | None = None, state: str | None = None, error: str | None = None):
        if error:
            safe_error = re.sub(r"[^A-Za-z0-9 _.-]", "", error)[:200]
            return HTMLResponse(f"<h2>Twitch authorization failed</h2><p>{safe_error}</p>", status_code=400)
        if not code or not state:
            return HTMLResponse("<h2>Missing Twitch OAuth code/state.</h2>", status_code=400)
        try:
            await twitch.exchange_code(code, state)
            await twitch.start()
            return HTMLResponse(
                "<h2>Готово.</h2><p>Twitch подключён. Можно закрыть это окно и вернуться в админку.</p>"
            )
        except Exception as e:
            safe_error = html.escape(str(e), quote=True)[:500]
            return HTMLResponse(
                f"<h2>Ошибка Twitch</h2><pre>{safe_error}</pre>",
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

    # ---- Subtitles ----
    @app.get("/api/subtitles/tracks")
    async def subtitle_tracks_state():
        return {"ok": True, "tracks": subtitle_tracks}

    @app.post("/api/subtitles/tracks")
    async def subtitle_tracks_update(req: dict):
        nonlocal subtitle_tracks
        tracks = req.get("tracks")
        if not isinstance(tracks, list) or not tracks or len(tracks) > 12:
            raise HTTPException(400, "Нужно от 1 до 12 дорожек субтитров")
        clean=[]
        seen=set()
        for raw in tracks:
            tid=str(raw.get("id", "")).strip().lower()
            name=str(raw.get("name", tid)).strip()[:64]
            lang=str(raw.get("language", "ru")).strip().lower()[:16]
            mode=str(raw.get("mode", "source")).strip()
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", tid) or tid in seen:
                raise HTTPException(400, f"Неверный или повторяющийся ID дорожки: {tid}")
            if mode == "whisper-translate":
                mode = "translate"
            if mode not in {"source", "translate"}:
                mode = "source"
            seen.add(tid)
            clean.append({"id": tid, "name": name or tid, "language": lang or "ru", "enabled": bool(raw.get("enabled", True)), "mode": mode})
        subtitle_tracks = clean
        db.set_setting("subtitle_tracks", json.dumps(subtitle_tracks, ensure_ascii=False))
        with subtitle_lock:
            active_ids = {t["id"] for t in subtitle_tracks}
            for t in subtitle_tracks:
                state = subtitle_state.setdefault(t["id"], {"text":"", "timestamp":0, "language":t["language"]})
                state["language"] = t["language"]
                if not t.get("enabled", True):
                    state.update({"text": "", "timestamp": 0})
            stale = [k for k in subtitle_state if k not in active_ids]
            for k in stale:
                subtitle_state.pop(k, None)
        source_language = db.get_setting("stt_language", "ru") or "ru"
        translation_status["message"] = "Подготовка переводчиков…"
        asyncio.create_task(asyncio.to_thread(translator.prepare_tracks, source_language, clean))
        return {"ok": True, "tracks": subtitle_tracks}

    @app.get("/api/subtitles/debug/{track_id}")
    async def subtitle_track_debug(track_id: str):
        track_id = str(track_id or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", track_id):
            raise HTTPException(400, "Invalid subtitle track id")
        with subtitle_lock:
            return {"ok": True, "track_id": track_id, "state": subtitle_state.get(track_id), "tracks": subtitle_tracks}

    @app.post("/api/subtitles/test/{track_id}")
    async def subtitle_track_test(track_id: str):
        track_id = str(track_id or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", track_id):
            raise HTTPException(400, "Invalid subtitle track id")
        with subtitle_lock:
            if track_id not in subtitle_state:
                raise HTTPException(404, "Subtitle track not found")
            ts = time.time()
            subtitle_state[track_id].update({
                "text": "Тест субтитров ✓",
                "timestamp": ts,
                "language": subtitle_state[track_id].get("language", "ru"),
            })
        return {"ok": True, "track_id": track_id, "text": "Тест субтитров ✓", "timestamp": ts}

    @app.post("/api/subtitles/test")
    async def subtitles_test():
        test_text = "Тест субтитров ✓"
        ts = time.time()
        on_subtitle({
            "source": True,
            "text": test_text,
            "language": "ru",
            "timestamp": ts,
        })
        return {"ok": True, "text": test_text, "timestamp": ts}

    @app.post("/api/subtitles/prepare")
    async def subtitles_prepare():
        source_language = db.get_setting("stt_language", "ru") or "ru"
        translation_status["message"] = "Подготовка переводчиков…"
        asyncio.create_task(asyncio.to_thread(translator.prepare_tracks, source_language, subtitle_tracks))
        return {"ok": True, "message": "Подготовка языковых пакетов запущена в фоне."}

    # ---- STT ----
    @app.post("/api/stt/config")
    async def stt_config(req: STTConfigRequest):
        current_chunk = stt.config.chunk_seconds
        current_overlap = stt.config.overlap_seconds
        if req.device_mode is not None and req.device_mode not in {"auto", "cuda", "cpu"}:
            raise HTTPException(400, "Unknown STT device mode")
        chunk = req.chunk_seconds if req.chunk_seconds is not None else current_chunk
        overlap = req.overlap_seconds if req.overlap_seconds is not None else current_overlap
        if overlap >= chunk:
            raise HTTPException(400, "STT overlap_seconds must be smaller than chunk_seconds")
        kwargs = req.model_dump(exclude_none=True)
        try:
            was_running = bool(stt.thread and stt.thread.is_alive())
            previous_model = stt.config.model_name
            previous_device = stt.config.device_mode
            stt.save_config(**kwargs)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

        changed_runtime = (
            was_running
            and (
                previous_model != stt.config.model_name
                or previous_device != stt.config.device_mode
            )
        )
        if changed_runtime:
            # Do not silently keep an old CUDA/CPU model after the user saved
            # a different device/model. Stop the current worker; the UI can
            # then start the newly selected configuration deterministically.
            stt.stop()

        return {"ok": True, "restart_required": changed_runtime, "state": stt.state()}

    @app.post("/api/stt/start")
    async def stt_start():
        try:
            # A running worker may still belong to a previous CPU/GPU
            # configuration (or may be alive after an inference error). In that
            # case, restart it instead of returning "already works".
            active = bool(stt.thread and stt.thread.is_alive())
            runtime_device = stt.runtime_device
            configured_device = stt.config.device_mode
            needs_restart = active and (
                bool(stt.last_error)
                or (
                    configured_device in {"cpu", "cuda"}
                    and runtime_device not in {None, configured_device}
                )
            )
            if needs_restart:
                stt.stop()
                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline:
                    if not (stt.thread and stt.thread.is_alive()) and not (
                        stt.start_thread and stt.start_thread.is_alive()
                    ):
                        break
                    await asyncio.sleep(0.2)
                if stt.thread and stt.thread.is_alive():
                    raise HTTPException(409, "Предыдущий STT ещё не остановился")
            stt.start()
            return {"ok": True, "state": stt.state(), "restarted": needs_restart}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.post("/api/stt/stop")
    async def stt_stop():
        stt.stop()
        return {"ok": True}

    return app
