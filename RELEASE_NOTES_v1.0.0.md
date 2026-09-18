# Stream Voice Bot v1.0.0

First public release.

## Included

- Silero V5 RU TTS with normal/loud profiles
- TTS queue controls and history
- Twitch OAuth and Channel Points text-to-TTS
- VK Video Live readonly chat bridge
- OBS audio routing through VB-CABLE
- Optional faster-whisper STT and subtitles
- Automatic Windows setup for Python, Python packages, PyTorch, Node.js dependencies and Silero model
- Local secret storage via Windows Credential Manager

## Important

The release ZIP does **not** contain the `v5_ru.pt` model. On first setup, the installer downloads it from Silero's official model host. Silero V5 models are licensed separately from this project under CC BY-NC-SA 4.0, including a non-commercial-use restriction.

VB-CABLE is a separate system audio driver and must be installed separately for OBS audio routing.
