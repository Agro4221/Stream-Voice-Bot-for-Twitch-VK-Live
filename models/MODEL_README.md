# Silero V5 RU

Файл `v5_ru.pt` намеренно не требуется для исходного репозитория: установщик умеет скачать его автоматически с официального URL Silero:

`https://models.silero.ai/models/tts/ru/v5_ru.pt`

Для offline/portable-пакета положите `v5_ru.pt` сюда и запустите:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\make_portable_release.ps1
```

Silero публикует список моделей и официальный URL `v5_ru.pt` в своём репозитории моделей. См. `README` и `models.yml` проекта `snakers4/silero-models`.
