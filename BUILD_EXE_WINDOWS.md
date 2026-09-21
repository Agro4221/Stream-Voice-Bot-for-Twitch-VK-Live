# Windows EXE package

Этот комплект собирается от релиза **v1.1.9** и не меняет рабочую логику Twitch, VK или STT.

В отличие от раннего прототипа упаковки здесь **нет второго launcher EXE и нет запуска EXE через временную папку PyInstaller one-file**. Пользовательский `StreamVoiceBot.exe` — это сразу само приложение. PyInstaller делает стандартный onedir-бандл; поддерживающие файлы находятся рядом с EXE. Это соответствует стандартной модели PyInstaller для onedir-сборки. citeturn344318search1turn344318search2

## Сборка

В PowerShell из корня проекта:

    powershell -ExecutionPolicy Bypass -File .\scripts\install_windows.ps1

Затем:

    powershell -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1

Готовый комплект:

    dist\StreamVoiceBot_1.1.9\

Запуск:

    dist\StreamVoiceBot_1.1.9\StreamVoiceBot.exe

## Распространение

Распространяй **всю папку** `StreamVoiceBot_1.1.9`, а не один EXE.

Внутри находятся:

- `StreamVoiceBot.exe`
- Python/ML зависимости PyInstaller
- встроенный Node.js runtime
- VK bridge и его `node_modules`
- `models\v5_ru.pt`
- web-интерфейс
- `VERSION`

Данные пользователя не входят в сборку: база и настройки создаются в `data\` рядом с комплектом при работе приложения.

STT-модель `large-v3-turbo` не копируется в пакет: при первом запуске STT она загружается в локальный кэш.

## Проверка

Перед запуском нового комплекта заверши старый экземпляр бота, чтобы порт 8787 был свободен.

Проверь:

1. Запуск `StreamVoiceBot.exe` и автоматическое открытие админки.
2. Обычную озвучку.
3. Twitch.
4. VK.
5. STT.
6. Очередь и кнопку «Очистить очередь».

При этом «Очистить очередь» относится к ожидающим элементам; уже идущая озвучка не отменяется.

## ZIP

Сборщик сознательно не использует Windows PowerShell `Compress-Archive` для этого большого ML-комплекта. Сначала проверяй саму папку `dist\StreamVoiceBot_1.1.9`; затем её можно архивировать 7-Zip.
