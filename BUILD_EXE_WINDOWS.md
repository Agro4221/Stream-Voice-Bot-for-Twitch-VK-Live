# Windows EXE package

Этот комплект собирается из версии **v1.1.9** и не меняет рабочую логику Twitch, VK или STT.

В пользовательском комплекте нет второго Core EXE. StreamVoiceBot.exe — единственный запускаемый файл приложения. Сборка использует стандартный PyInstaller onedir. PyInstaller 6.x по умолчанию помещает supporting-файлы в _internal, но spec явно выставляет contents_directory=".", чтобы текущий код мог находить ресурсы относительно папки EXE. citeturn614481search0turn614481search4

## Сборка

Из корня проекта:

    powershell -ExecutionPolicy Bypass -File .\scripts\install_windows.ps1

Затем:

    powershell -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1

Сборщик создаёт отдельную папку:

    dist_release\StreamVoiceBot_1.1.9\

Существующий dist\ не изменяется, поэтому ранее проверенный локальный комплект остаётся резервом.

## Почему сборка использует отдельный build environment

Твой рабочий .venv может содержать CUDA-сборку PyTorch. Для релиза это избыточно: Silero TTS в приложении работает через PyTorch CPU.

Поэтому build_exe.ps1 автоматически создаёт .build_venv и устанавливает туда CPU-only PyTorch. Это отделяет упаковку релиза от GPU-окружения разработчика и не требует менять рабочий .venv.

PyTorch публикует отдельные CPU wheels для Windows x64; сборщик использует официальный CPU index. citeturn298888search1

## Что входит в пакет

- StreamVoiceBot.exe
- models\v5_ru.pt — Silero V5 уже внутри комплекта
- PyTorch CPU runtime для Silero
- faster-whisper / ctranslate2
- встроенный Node.js runtime
- VK bridge и его production node_modules
- web-интерфейс
- VERSION

Локальные базы, Twitch tokens, пользовательские настройки и кэши в пакет не копируются.

STT-модель large-v3-turbo загружается в пользовательский Hugging Face cache при первом запуске STT. Дополнительные языковые пакеты Argos Translate загружаются только по запросу.

## Проверка перед выпуском

Перед публикацией нового комплекта нужно проверить на Windows:

1. StreamVoiceBot.exe запускается и открывает админку.
2. TTS воспроизводит голос и находит встроенный models\v5_ru.pt.
3. Twitch можно настроить на новом ПК; токены не входят в релиз.
4. VK bridge запускается из встроенного Node runtime.
5. STT запускается и работает через доступный GPU/CPU путь.
6. Админка работает.
7. Порт 8787 освобождён при завершении приложения.

После успешной проверки архивируется вся папка StreamVoiceBot_1.1.9.

## Важно для релиза

Не публикуй:

- .venv
- .build_venv
- .cache
- build
- старые папки dist
- data
- .runtime\downloads

Публиковать нужно только готовую versioned-папку из dist_release.
