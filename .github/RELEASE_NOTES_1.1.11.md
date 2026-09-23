# Stream Voice Bot v1.1.11

## Что нового

- Twitch Device Code Flow и стабильное подключение Twitch EventSub.
- VK Video Live: readonly-чат через актуальный live.vkvideo.ru bridge.
- Silero V5 RU с двумя профилями озвучки, очередью, управлением и выводом через VB-CABLE.
- Опциональные субтитры для OBS через отдельные Browser Source-дорожки.
- faster-whisper STT с режимами `Авто (CUDA → CPU)`, `GPU (NVIDIA CUDA)` и `CPU int8`.
- Живые STT-окна с ограничением очереди аудио, диагностикой микрофона и защитой от типичных Whisper hallucination credit-фраз.
- Перевод субтитров локально через Argos Translate.
- Portable onedir EXE: пользовательская папка содержит только EXE, `_internal`, `data` и README.
- Сборка сокращена до примерно 4 тысяч файлов вместо прежнего раздутого набора.

## Windows

Распакуй архив целиком и запусти `StreamVoiceBot.exe`.

Для GPU-режима приложение может один раз подготовить CUDA 12 + cuDNN 9 runtime в `data/gpu_runtime`.
После первого запуска этот runtime повторно скачиваться не должен.

## OBS subtitles

Для каждой дорожки используется отдельный URL вида:

`http://127.0.0.1:8787/subtitles/ru`

Тестировать дорожку можно непосредственно кнопкой `Тест` в админке.

## Примечание

Первый запуск Whisper-модели может потребовать дополнительную загрузку модели. GPU runtime загружается отдельно только при необходимости GPU-режима.
