# Безопасность

Stream Voice Bot рассчитан на локальный запуск Windows.

## Секреты

Twitch Client Secret, Twitch access/refresh tokens и VK SERVICE/SECURE keys должны храниться через Windows Credential Manager. Не помещайте их в `SQLite`, `.env`, README, GitHub Issues или коммиты.

## Что не нужно коммитить

```text
.venv/
node_modules/
__pycache__/
data/*.sqlite3
*.wav
*.log
```

Локальные настройки и история находятся в `data/` и защищены `.gitignore`.

## Перед публикацией GitHub

Проверь:

```powershell
git status --ignored
git ls-files | findstr /I "secret token sqlite wav .venv node_modules"
```

В Git не должно попасть ничего из списка приватных данных.

## Сообщить об уязвимости

Для приватных сообщений с данными, которые не стоит публично обсуждать, используй приватный канал связи владельца репозитория. Публичный Issue подходит для обычных багов и ошибок установки, но не для раскрытия секретов или персональных данных.
