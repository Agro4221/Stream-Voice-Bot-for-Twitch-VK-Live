import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StabilitySourceContractTests(unittest.TestCase):
    def read(self, path):
        return (ROOT / path).read_text(encoding="utf-8")

    def test_tts_controls_are_present(self):
        src = self.read("stream_voice_bot/tts.py")
        self.assertIn("def test_tone(self, frequency: float = 880.0, duration: float = 0.35, volume_db: float | None = None)", src)
        self.assertIn("numerator = max(1, int(round(100.0 / speed)))", src)
        self.assertIn("resample_poly(audio, numerator, 100)", src)
        self.assertIn("Do not clear stop/skip here", src)
        self.assertIn("active = self.current is not None", src)
        self.assertIn("if active:", src)
        self.assertIn("self.cancelled_ids.update(removed_ids)", src)

    def test_twitch_dcf_and_reconnect_contract(self):
        src = self.read("stream_voice_bot/twitch.py")
        self.assertIn('"scopes": " ".join(self.REQUIRED_SCOPES)', src)
        self.assertIn("await client.post(OAUTH_TOKEN, data=params)", src)
        self.assertIn("await self._open_eventsub_socket(reconnect_url)", src)
        self.assertIn("await old_ws.close()", src)

    def test_runtime_lifecycle_and_launcher_contract(self):
        main = self.read("stream_voice_bot/__main__.py")
        launcher = self.read("start_bot.bat")
        web = self.read("stream_voice_bot/web/index.html")
        hidden_launcher = self.read("scripts/launch_bot.ps1")

        self.assertIn("access_log=False", main)
        self.assertIn('log_level="warning"', main)
        self.assertIn("app.state.server = server", main)

        self.assertIn('Start-Process -FilePath $PythonExe', hidden_launcher)
        self.assertIn("Start-Process $Url", hidden_launcher)
        self.assertIn("127.0.0.1:8787", hidden_launcher)

        self.assertIn("function queueAction(action)", web)
        self.assertIn("const cleared=Number(result.cleared||0)", web)
        self.assertIn("Очередь очищена: убрано", web)
        self.assertIn("method:'POST'", web)
        self.assertIn('onclick="shutdownBot()"', web)
        self.assertIn("Работает", web)
        self.assertIn("Выключен", web)
        self.assertIn("только награда", web)
        self.assertIn("keepalive:true", web)
        self.assertIn("refreshTimer=null", web)
        self.assertIn("let logTimer=null", web)
        self.assertIn("api('/api/logs?limit=250')", web)
        self.assertIn("api('/api/logs/clear'", web)

        self.assertIn('start "" /b powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\\launch_bot.ps1"', launcher)
        self.assertIn("exit /b 0", launcher)
        self.assertNotIn("python.exe -m stream_voice_bot", launcher.split("start_bot.bat")[-1])

        bat_files = sorted(
            p.relative_to(ROOT).as_posix()
            for p in ROOT.rglob("*.bat")
            if ".git" not in p.parts and ".venv" not in p.parts and ".build_venv" not in p.parts
        )
        self.assertEqual(bat_files, ["start_bot.bat"])


    def test_vk_bridge_contract(self):
        src = self.read("stream_voice_bot/vk_bridge/bridge.js")
        self.assertIn('import VKPLMessageClient from "vklive-message-client";', src)
        self.assertIn('auth: "readonly"', src)
        self.assertIn('channels: [channel]', src)
        self.assertIn('client.on("message"', src)
        self.assertIn('ctx?.message?.text', src)
        self.assertIn('vklive-message-client@5.3.2', src)
        self.assertIn("--self-test", src)


    def test_app_and_db_stability_contract(self):
        app = self.read("stream_voice_bot/app.py")
        db = self.read("stream_voice_bot/db.py")
        self.assertIn('db.claim_event("twitch:" + str(dedupe_id))', app)
        self.assertIn("Normal Twitch chat is intentionally ignored", app)
        self.assertIn("from .models import QueueItem, utc_now", app)
        self.assertIn('QueueItem(text, username, "vkplay-reward", created_at=created_at)', app)
        self.assertIn("parse_vk_reward_announcement(raw_text)", app)
        self.assertIn('Ordinary viewer messages are never TTS input.', app)
        vk = self.read("stream_voice_bot/vkplay.py")
        self.assertIn("ChatBot:", vk)
        self.assertIn("Озвучить\\s+сообщение", vk)
        self.assertIn("from .vkplay import VKPlayService, parse_vk_reward_announcement", app)
        self.assertIn('"cleared", "audio_error"', app)
        self.assertIn("def claim_event", db)
        self.assertIn("def mark_pending_history", db)
        self.assertIn("def clear_history", db)
        self.assertIn('@app.delete("/api/history")', app)
        self.assertIn('@app.post("/api/shutdown")', app)
        self.assertIn("server.should_exit = True", app)
        self.assertIn("from .log_buffer import get_logs, install as install_log_buffer", app)
        self.assertIn('@app.get("/api/logs")', app)
        self.assertIn('@app.post("/api/logs/clear")', app)

    def test_stt_status_state_is_authoritative(self):
        app = self.read("stream_voice_bot/app.py")
        self.assertIn('"stt": {**stt_status, **stt.state()}', app)
        self.assertIn("The service state is authoritative.", app)

    def test_stt_uses_t_one_streaming_backend(self):
        stt = self.read("stream_voice_bot/stt.py")
        self.assertIn("sherpa_onnx.OnlineRecognizer.from_t_one_ctc", stt)
        self.assertIn("T_ONE_MODEL_RATE = 8000", stt)
        self.assertIn('provider="cuda"', stt)
        self.assertIn('provider="cpu"', stt)
        self.assertIn('decoding_method="greedy_search"', stt)
        self.assertIn("accept_waveform(T_ONE_MODEL_RATE", stt)
        self.assertIn("decode_stream", stt)

    def test_stt_cuda_is_optional_and_falls_back(self):
        stt = self.read("stream_voice_bot/stt.py")
        self.assertIn("def _nvidia_gpu_present", stt)
        self.assertIn("def _gpu_runtime_is_ready", stt)
        self.assertIn("self._ensure_gpu_runtime()", stt)
        self.assertIn("CUDA STT недоступна", stt)
        self.assertIn('self._build_recognizer("cpu")', stt)

    def test_stt_no_longer_depends_on_whisper_stack(self):
        stt = self.read("stream_voice_bot/stt.py")
        requirements = self.read("requirements.txt")
        self.assertNotIn("faster_whisper", stt)
        self.assertNotIn("ctranslate2", stt)
        self.assertNotIn("huggingface_hub", stt)
        self.assertNotIn("faster-whisper", requirements)
        self.assertNotIn("ctranslate2", requirements)
        self.assertIn("sherpa-onnx==1.13.8", requirements)

    def test_stt_status_reports_backend(self):
        stt = self.read("stream_voice_bot/stt.py")
        web = self.read("stream_voice_bot/web/index.html")
        self.assertIn('"engine": "T-one / sherpa-onnx"', stt)
        self.assertIn('"provider": self.runtime_device', stt)
        self.assertIn("sttProvider", web)
        self.assertIn("NVIDIA CUDA", web)
        self.assertIn("CPU fallback", web)

    def test_stt_startup_errors_are_visible_and_model_archive_can_extract(self):
        stt = self.read("stream_voice_bot/stt.py")
        self.assertIn("import tarfile", stt)
        self.assertIn("STT startup failed", stt)
        self.assertIn('message=f"Ошибка запуска STT: {detail}"', stt)
        self.assertIn('loading_phase = "error"', stt)

    def test_subtitle_tests_cover_translation_tracks_and_stale_results(self):
        app = self.read("stream_voice_bot/app.py")
        stt = self.read("stream_voice_bot/stt.py")
        web = self.read("stream_voice_bot/web/index.html")
        self.assertIn("translator.translate", app)
        self.assertIn("translation_tracks_pending", app)
        self.assertIn("incoming_sequence", app)
        self.assertIn("sequence_id=sequence", stt)
        self.assertIn("self.translation_latest", stt)
        self.assertIn("pending.length", web)

    def test_stt_runtime_diagnostics_and_restart_contract(self):
        stt = self.read("stream_voice_bot/stt.py")
        app = self.read("stream_voice_bot/app.py")
        web = self.read("stream_voice_bot/web/index.html")
        self.assertIn("self.audio_rms", stt)
        self.assertIn("self.transcribe_attempts", stt)
        self.assertIn('"audio_rms":', stt)
        self.assertIn("restart_required", app)
        self.assertIn("previous_input != stt.config.input_device", app)
        self.assertIn("stt.stop()", app)
        self.assertIn("Микрофон: RMS", web)
        self.assertIn("await api('/api/stt/start'", web)
        self.assertNotIn('id="vkStatus"', web)

    def test_stt_source_subtitles_are_not_routed_by_detected_language(self):
        stt = self.read("stream_voice_bot/stt.py")
        app = self.read("stream_voice_bot/app.py")
        web = self.read("stream_voice_bot/web/index.html")
        self.assertIn('"source": True', stt)
        self.assertIn('"source": False', stt)
        self.assertIn("is_translated = data.get(\"source\") is False", app)
        self.assertIn("Original STT text always goes to the enabled source tracks.", app)
        self.assertIn("status-good", web)
        self.assertIn("status-bad", web)
        self.assertIn("status-pending", web)

    def test_stt_is_bounded_and_uses_streaming_endpointing(self):
        src = self.read("stream_voice_bot/stt.py")
        self.assertIn("deque(maxlen=200)", src)
        self.assertIn("def _candidate_input_rates", src)
        self.assertIn("def _open_input_stream", src)
        self.assertIn("stream = sd.InputStream(", src)
        self.assertIn("CoInitialize(None)", src)
        self.assertIn("CoUninitialize()", src)
        self.assertIn("source_audio = np.concatenate(queued)", src)
        self.assertIn("enable_endpoint_detection=True", src)
        self.assertIn("self.recognizer.is_endpoint(model_stream)", src)
        self.assertIn("self.recognizer.reset(model_stream)", src)
        self.assertIn("self.translator.translate(", src)
        self.assertIn("stt-translate-", src)

    def test_stt_credit_filter_is_defense_in_depth(self):
        stt = self.read("stream_voice_bot/stt.py")
        app = self.read("stream_voice_bot/app.py")
        subtitles = self.read("stream_voice_bot/web/subtitles.html")
        self.assertIn("_HALLUCINATION_CREDIT_RE", stt)
        self.assertIn("_is_bad_subtitle_text", app)
        self.assertIn(r"\bdima\s*torzok\b", app)
        self.assertIn("const blocked=", subtitles)

    def test_stt_cuda_runtime_is_local(self):
        stt = self.read("stream_voice_bot/stt.py")
        web = self.read("stream_voice_bot/web/index.html")
        self.assertIn("def _install_gpu_runtime", stt)
        self.assertIn("cublas64_12.dll", stt)
        self.assertIn("cudnn64_9.dll", stt)
        self.assertIn("data/gpu_runtime", stt)
        self.assertIn("T-one / sherpa-onnx", web)

    def test_subtitle_browser_diagnostics_are_wired(self):
        web = self.read("stream_voice_bot/web/index.html")
        app = self.read("stream_voice_bot/app.py")
        subtitles = self.read("stream_voice_bot/web/subtitles.html")
        self.assertIn("data-id=", web)
        self.assertIn("testSubtitleTrack(this.closest('.subtrack').dataset.id)", web)
        self.assertIn('@app.get("/api/subtitles/debug/{track_id}")', app)
        self.assertIn("URLSearchParams(location.search).get('debug')==='1'", subtitles)


    def test_subtitle_credit_hallucinations_are_blocked_end_to_end(self):
        app = self.read("stream_voice_bot/app.py")
        subtitles = self.read("stream_voice_bot/web/subtitles.html")
        self.assertIn("_is_bad_subtitle_text", app)
        self.assertIn(r"\bdima\s*torzok\b", app)
        self.assertIn("if not text_value or _is_bad_subtitle_text(text_value)", app)
        self.assertIn("const blocked=", subtitles)
        self.assertIn(r"dima\s*torzok", subtitles)


    def test_stt_can_install_local_cuda_runtime_on_demand(self):
        stt = self.read("stream_voice_bot/stt.py")
        web = self.read("stream_voice_bot/web/index.html")
        self.assertIn("def _install_gpu_runtime", stt)
        self.assertIn("cublas64_12.dll", stt)
        self.assertIn("cudnn64_9.dll", stt)
        self.assertIn('[tar_exe, "-xf"', stt)
        self.assertIn("data/gpu_runtime", web)
        self.assertIn("849 МБ", web)


    def test_gpu_runtime_uses_database_data_directory(self):
        stt = self.read("stream_voice_bot/stt.py")
        self.assertIn("self.db.path.parent", stt)
        self.assertIn('runtime_dir = Path(data_root) / "gpu_runtime"', stt)


    def test_gpu_runtime_path_is_added_to_windows_dll_search(self):
        stt = self.read("stream_voice_bot/stt.py")
        self.assertIn("local_runtime = self._gpu_runtime_path()", stt)
        self.assertIn("candidates.append(local_runtime)", stt)
        self.assertIn("runtime_dir = Path(data_root) / \"gpu_runtime\"", stt)


    def test_stt_checks_nvidia_gpu_before_runtime_download(self):
        stt = self.read("stream_voice_bot/stt.py")
        web = self.read("stream_voice_bot/web/index.html")
        self.assertIn("def _nvidia_gpu_present(self)", stt)
        self.assertIn("if not self._nvidia_gpu_present():", stt)
        self.assertIn("GPU runtime не скачивается.", stt)
        self.assertIn("проверяет наличие NVIDIA GPU", web)



if __name__ == "__main__":
    unittest.main()
