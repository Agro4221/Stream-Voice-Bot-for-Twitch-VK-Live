from __future__ import annotations

import keyring

SERVICE = "StreamVoiceBot"


class SecretStore:
    """Persistent local secret storage via the Windows credential manager."""

    def get(self, name: str) -> str:
        try:
            return keyring.get_password(SERVICE, name) or ""
        except Exception:
            return ""

    def set(self, name: str, value: str) -> bool:
        value = (value or "").strip()
        if not value:
            return False
        keyring.set_password(SERVICE, name, value)
        # Verify immediately. This makes a broken keyring backend visible
        # instead of silently losing a Twitch/VK credential.
        stored = keyring.get_password(SERVICE, name) or ""
        if stored != value:
            raise RuntimeError(
                f"Credential store verification failed for '{name}'. "
                "Check Windows Credential Manager/keyring backend."
            )
        return True

    def delete(self, name: str) -> None:
        try:
            keyring.delete_password(SERVICE, name)
        except Exception:
            pass

    def has(self, name: str) -> bool:
        return bool(self.get(name))
