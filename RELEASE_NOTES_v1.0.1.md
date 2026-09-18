# Stream Voice Bot v1.0.1

- Twitch Device Code Flow: connect with Client ID + browser authorization; Client Secret is no longer required for new installs.
- Silero profile volume changed to a real dB slider with normalization and peak limiting.
- Preserves Ё/ё and common Unicode punctuation/stress marks during TTS normalization.
- Added multi-track OBS subtitle URLs and subtitle-track manager.
- Reduced empty vertical space in the current queue card.
- VK Video Live status is shown alongside Twitch and STT in the Integration card.


Subtitle tracks currently support source text fan-out and local Whisper → English translation; arbitrary target-language translation is intentionally not bundled yet.
