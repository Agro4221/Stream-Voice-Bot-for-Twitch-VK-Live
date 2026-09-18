from __future__ import annotations

import threading
from typing import Callable


class TranslationService:
    """Offline subtitle translation using Argos Translate.

    Translation packages are downloaded on demand from the public Argos
    package index and installed locally. Direct pairs are preferred; when a
    direct pair is unavailable, the service prepares a route through English,
    which Argos Translate supports via pivot translation.
    """

    def __init__(self, on_status: Callable[[dict], None] | None = None):
        self.on_status = on_status or (lambda data: None)
        self._lock = threading.RLock()
        self._cache: dict[tuple[str, str], object] = {}
        self._index_ready = False

    @staticmethod
    def _base(code: str) -> str:
        return str(code or "").strip().lower().split("-")[0]

    def _status(self, message: str, **extra):
        self.on_status({"message": message, **extra})

    def _imports(self):
        try:
            import argostranslate.package as package
            import argostranslate.translate as translate
        except Exception as e:
            raise RuntimeError(
                "Переводчик не установлен. Запусти установку/обновление зависимостей (требуется argostranslate)."
            ) from e
        return package, translate

    def _installed_languages(self, translate_module):
        return translate_module.get_installed_languages()

    def _get_installed_translation(self, translate_module, source: str, target: str):
        source = self._base(source)
        target = self._base(target)
        languages = self._installed_languages(translate_module)
        from_lang = next((x for x in languages if self._base(x.code) == source), None)
        to_lang = next((x for x in languages if self._base(x.code) == target), None)
        if not from_lang or not to_lang:
            return None
        try:
            return from_lang.get_translation(to_lang)
        except Exception:
            return None

    def _install_pair(self, package_module, source: str, target: str) -> bool:
        source = self._base(source)
        target = self._base(target)
        if source == target:
            return True
        available = package_module.get_available_packages()
        candidates = [
            p for p in available
            if self._base(getattr(p, "from_code", "")) == source
            and self._base(getattr(p, "to_code", "")) == target
        ]
        if not candidates:
            return False
        # Prefer the newest package when the index exposes multiple versions.
        candidates.sort(key=lambda p: str(getattr(p, "package_version", "")), reverse=True)
        pkg = candidates[0]
        self._status(f"Скачивание переводчика {source} → {target}...")
        path = pkg.download()
        package_module.install_from_path(path)
        return True

    def _ensure_index(self, package_module):
        if not self._index_ready:
            self._status("Обновление списка языковых пакетов…")
            package_module.update_package_index()
            self._index_ready = True

    def ensure(self, source: str, target: str):
        source = self._base(source)
        target = self._base(target)
        if not source or not target:
            raise ValueError("Не задан исходный или целевой язык перевода")
        if source == target:
            return None
        key = (source, target)
        with self._lock:
            if key in self._cache:
                return self._cache[key]

            package, translate = self._imports()
            direct = self._get_installed_translation(translate, source, target)
            if direct is not None:
                self._cache[key] = direct
                return direct

            self._ensure_index(package)
            if self._install_pair(package, source, target):
                direct = self._get_installed_translation(translate, source, target)
                if direct is not None:
                    self._cache[key] = direct
                    return direct

            # Argos can pivot through English when source→en and en→target
            # packages are installed. This is useful for ru→de/pl/es/etc.
            if source != "en" and target != "en":
                self._install_pair(package, source, "en") or None
                self._install_pair(package, "en", target) or None
                pivot = self._get_installed_translation(translate, source, target)
                if pivot is not None:
                    self._cache[key] = pivot
                    return pivot

            raise RuntimeError(
                f"Не удалось подготовить перевод {source} → {target}. "
                "Для этой языковой пары нет доступного маршрута в Argos."
            )

    def translate(self, text: str, source: str, target: str) -> str:
        text = str(text or "").strip()
        if not text:
            return ""
        source = self._base(source)
        target = self._base(target)
        if source == target:
            return text
        translation = self.ensure(source, target)
        if translation is None:
            return text
        return str(translation.translate(text) or "").strip()

    def prepare_tracks(self, source: str, tracks: list[dict]):
        targets = []
        for track in tracks:
            if not track.get("enabled", True):
                continue
            if str(track.get("mode", "source")) != "translate":
                continue
            target = self._base(track.get("language", ""))
            if target and target != self._base(source):
                targets.append(target)
        for target in dict.fromkeys(targets):
            try:
                self.ensure(source, target)
            except Exception as e:
                self._status(f"Перевод {source} → {target}: ошибка: {type(e).__name__}: {e}")
            else:
                self._status(f"Переводчик {source} → {target} готов ✓")
