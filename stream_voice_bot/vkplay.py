from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

log = logging.getLogger("stream_voice_bot.vkplay")

BRIDGE_JS = Path(__file__).resolve().parent / "vk_bridge" / "bridge.js"


def normalize_channel(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""

    # Accept either:
    #   username
    #   live.vkvideo.ru/username
    #   https://live.vkvideo.ru/username
    raw = value.replace("https://", "").replace("http://", "")
    raw = raw.split("/", 1)[-1] if "/" in raw else raw
    raw = raw.split("?", 1)[0].split("#", 1)[0].strip().strip("/")
    return raw


VK_REWARD_ANNOUNCEMENT_RE = re.compile(
    r"^\s*\*{0,2}(?:ChatBot:\s*)?(?P<username>[^*\r\n]+?)\s*\*{0,2}\s+"
    r"получает\s+награду:\s*Озвучить\s+сообщение\s+за\s+"
    r"\d[\d\s.,]*\s*:?[\s\r\n]*(?P<text>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def parse_vk_reward_announcement(text: str) -> dict | None:
    """Extract only viewer text from VK's ChatBot reward system notice."""
    value = (text or "").strip()
    match = VK_REWARD_ANNOUNCEMENT_RE.match(value)
    if not match:
        return None
    username = match.group("username").strip()
    user_text = match.group("text").strip()
    if not username or not user_text:
        return None
    return {"username": username, "text": user_text}



class VKPlayService:
    """
    VK Video Live chat adapter.

    The previous adapter used an older reverse-engineered package and passed
    the stored channel value straight into it. The current VK Video Live
    platform is at live.vkvideo.ru, and a maintained chat client supports a
    readonly mode that does not require a user token. We therefore use the
    chat client for receiving messages and keep VK service credentials
    separate for future DevAPI operations.
    """

    def __init__(self, db, on_chat, on_status):
        self.db = db
        self.on_chat = on_chat
        self.on_status = on_status
        self.proc = None
        self.reader_task = None
        self.stderr_task = None
        self.supervisor_task = None
        self.running = False
        self.last_stderr = ""

    def configured(self) -> bool:
        return bool(normalize_channel(self.db.get_setting("vkplay_channel_id", "")))

    def node_command(self) -> str:
        project_root = BRIDGE_JS.parent.parent.parent
        local_node = project_root / ".runtime" / "node" / "node.exe"
        if local_node.exists():
            return str(local_node)
        system_node = shutil.which("node")
        if system_node:
            return system_node
        raise RuntimeError("Node.js не найден. Запусти scripts\\install_windows.ps1")

    async def start(self):
        if self.running:
            return

        channel = normalize_channel(self.db.get_setting("vkplay_channel_id", ""))
        if not channel:
            raise RuntimeError(
                "Укажи логин канала VK Видео Live или ссылку вида "
                "https://live.vkvideo.ru/username"
            )
        if not BRIDGE_JS.exists():
            raise RuntimeError("VK Video Live bridge is missing")
        self.running = True
        self.supervisor_task = asyncio.create_task(
            self._supervise(channel),
            name="vkvideo-supervisor",
        )

    async def stop(self):
        self.running = False

        if self.supervisor_task:
            self.supervisor_task.cancel()
            try:
                await self.supervisor_task
            except asyncio.CancelledError:
                pass
        self.supervisor_task = None

        await self._stop_process()
        self.on_status({"connected": False, "message": "VK Video Live stopped"})

    async def _stop_process(self):
        if self.reader_task:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass
        self.reader_task = None

        if self.stderr_task:
            self.stderr_task.cancel()
            try:
                await self.stderr_task
            except asyncio.CancelledError:
                pass
        self.stderr_task = None

        proc = self.proc
        self.proc = None
        if proc:
            try:
                proc.terminate()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    async def _spawn(self, channel: str):
        self.proc = await asyncio.create_subprocess_exec(
            self.node_command(),
            str(BRIDGE_JS),
            channel,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.reader_task = asyncio.create_task(
            self._read_stdout(self.proc),
            name="vkvideo-stdout",
        )
        self.stderr_task = asyncio.create_task(
            self._read_stderr(self.proc),
            name="vkvideo-stderr",
        )

        self.on_status({
            "connected": True,
            "message": f"VK Video Live bridge started: {channel}",
            "channel": channel,
        })

    async def _supervise(self, channel: str):
        backoff = 1.0
        while self.running:
            self.last_stderr = ""
            try:
                await self._spawn(channel)
                assert self.proc is not None
                return_code = await self.proc.wait()

                if not self.running:
                    return

                detail = f" Последняя ошибка: {self.last_stderr}" if self.last_stderr else ""
                self.on_status({
                    "connected": False,
                    "message": (
                        f"VK Video Live bridge завершился (code={return_code})."
                        f"{detail} Перезапуск через {backoff:g} сек."
                    ),
                })
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.on_status({
                    "connected": False,
                    "message": f"VK Video Live bridge error: {type(e).__name__}: {e}",
                })

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 20)

    async def _read_stdout(self, proc):
        assert proc.stdout is not None
        while self.running:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                item = json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                continue

            if item.get("type") == "status":
                self.on_status(item)
            elif item.get("type") == "chat":
                try:
                    self.on_chat(item)
                except Exception:
                    log.exception("VK Video Live chat handler failed")

    async def _read_stderr(self, proc):
        assert proc.stderr is not None
        while self.running:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                self.last_stderr = text
                self.on_status({
                    "connected": self.proc is proc,
                    "message": text,
                })
