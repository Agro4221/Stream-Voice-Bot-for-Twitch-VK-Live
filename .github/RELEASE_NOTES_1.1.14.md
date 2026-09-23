# v1.1.14 candidate — STT v2

This candidate replaces the previous fixed-window subtitle engine with a new lightweight pipeline.

## What changed

- STT v2 uses a lightweight local VAD before Whisper.
- Whisper runs only after a speech phrase is detected and finished with a short silence.
- Audio and phrase queues are bounded; old work is dropped instead of accumulating.
- Default STT model is now `small` instead of `large-v3-turbo`.
- Stopping STT unloads the Whisper model from memory.
- GPU/CUDA support remains available, with automatic CPU fallback in `auto` mode.
- The previous CUDA runtime fixes remain included.
- Subtitle translation remains supported.

## Important

Subtitles are still experimental and may need tuning for individual microphones and rooms. TTS is not changed by this rewrite.

This is a candidate build for manual stream testing, not a final release.
