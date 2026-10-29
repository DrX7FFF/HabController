"""Notifications du système : messages en attente, signalés par une pastille rouge sur l'onglet
Administration (envoyées aux pages par le WebSocket, voir main.py). Chaque source (surveillance,
alerte…) pose ou retire les siennes par identifiant ; une notification reste tant que sa source
ne l'a pas retirée."""

import time
from collections.abc import Callable


class Notifications:
    def __init__(self, on_change: Callable[[], None] = lambda: None):
        self._items: dict[str, dict] = {}
        self._on_change = on_change  # appelé à chaque changement de la liste

    def set(self, key: str, text: str, level: str = "info") -> None:
        """Pose (ou met à jour) la notification `key` ; level : "info" ou "error"."""
        current = self._items.get(key)
        since = current["since"] if current else int(time.time() * 1000)
        item = {"id": key, "text": text, "level": level, "since": since}
        if item != current:
            self._items[key] = item
            self._on_change()

    def clear(self, key: str) -> None:
        if self._items.pop(key, None):
            self._on_change()

    def get(self, key: str) -> dict | None:
        return self._items.get(key)

    def list(self) -> list[dict]:
        return sorted(self._items.values(), key=lambda n: n["since"])
