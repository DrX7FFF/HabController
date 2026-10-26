"""Notifications du système : messages en attente, signalés par une pastille rouge sur l'onglet
Administration (GET /api/notifications). Chaque source (surveillance, alerte…) pose ou retire
les siennes par identifiant ; une notification reste tant que sa source ne l'a pas retirée."""

import time


class Notifications:
    def __init__(self):
        self._items: dict[str, dict] = {}

    def set(self, key: str, text: str, level: str = "info") -> None:
        """Pose (ou met à jour) la notification `key` ; level : "info" ou "error"."""
        current = self._items.get(key)
        since = current["since"] if current else int(time.time() * 1000)
        self._items[key] = {"id": key, "text": text, "level": level, "since": since}

    def clear(self, key: str) -> None:
        self._items.pop(key, None)

    def get(self, key: str) -> dict | None:
        return self._items.get(key)

    def list(self) -> list[dict]:
        return sorted(self._items.values(), key=lambda n: n["since"])
