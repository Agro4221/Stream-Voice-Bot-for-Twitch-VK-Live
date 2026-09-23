# Third-party notices

Stream Voice Bot is MIT-licensed. This file documents notable third-party
components used by the project; each component remains subject to its own
license and terms.

## Silero TTS / `v5_ru.pt`

The Russian V5 model is downloaded from the official Silero model host:
https://models.silero.ai/models/tts/ru/v5_ru.pt

Silero publishes these V5 models under the project's Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0) license.
See:
- https://github.com/snakers4/silero-models
- https://github.com/snakers4/silero-models/blob/master/LICENSE

The `v5_ru.pt` model is **not** relicensed under this project's MIT license.

## PyTorch

Used by the local TTS stack.
https://github.com/pytorch/pytorch

See the upstream repository for the applicable license and notices.

## faster-whisper

Used for optional speech-to-text. The upstream project identifies itself as MIT
licensed.
https://github.com/SYSTRAN/faster-whisper

## TwitchIO

Used for Twitch integration.
https://github.com/PythonistaGuild/TwitchIO

See the upstream repository for the applicable license and notices.

## `vklive-message-client`

Used by the readonly VK Video Live Node bridge.
https://www.npmjs.com/package/vklive-message-client

See the package metadata and upstream project for the current license.

## NVIDIA CUDA / cuBLAS / cuDNN runtime

The optional STT GPU mode uses NVIDIA CUDA 12 runtime libraries and cuDNN 9.
When the required runtime is not found on Windows, the application can download
a pinned CUDA 12 + cuDNN 9 runtime archive from the Purfview
whisper-standalone-win project into the user's writable `data/gpu_runtime`
directory. These NVIDIA binaries are not included in the Git repository.

Upstream references:
- https://github.com/SYSTRAN/faster-whisper
- https://github.com/Purfview/whisper-standalone-win/releases/tag/libs
- https://docs.nvidia.com/deeplearning/cudnn/installation/latest/windows.html
## Python audio dependencies

The project also uses NumPy, SciPy, SoundDevice and SoundFile. Their respective
licenses remain applicable to the installed packages.

## Argos Translate

Used for optional local multilingual subtitle translation.
https://github.com/argosopentech/argos-translate

The Argos Translate library is MIT/CC0 licensed according to its upstream project. Individual translation model packages are distributed separately and may carry their own licensing terms; users should review the license metadata for each language package they install.
