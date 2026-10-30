"""Firmwares plus récents proposés par les équipements, installés à la demande, un à la fois.

Un pilote propose le sien (offer) et le retire quand il n'y en a plus (withdraw) ; install()
appelle sa méthode update_firmware(progress), qui lève DeviceError en cas d'échec et rend la main
une fois le firmware installé. on_change est appelé à chaque changement (liste, avancement)."""

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from core.base import DeviceError

log = logging.getLogger(__name__)


class Updatable(Protocol):
    id: str

    def update_firmware(self, progress: Callable[[float], None]) -> Awaitable[None]: ...


class Firmwares:
    def __init__(self):
        self.on_change: Callable[[], None] = lambda: None
        self._offers: dict[str, tuple[Updatable, str, str]] = {}  # id : (équipement, actuel, proposé)
        self.running: str | None = None  # équipement en cours d'installation
        self.progress = 0  # %, de l'installation en cours
        self.last = ""  # résultat de la dernière installation

    def offer(self, device: Updatable, current: str, version: str) -> None:
        changed = self._offers.get(device.id, (None, "", ""))[1:] != (current, version)
        self._offers[device.id] = (device, current, version)
        if changed:
            log.info("%s : firmware %s disponible (actuel %s)", device.id, version, current)
            self.on_change()

    def withdraw(self, dev_id: str) -> None:
        if self._offers.pop(dev_id, None):
            self.on_change()

    def list(self) -> dict:
        offers = [{"device": i, "current": c, "version": v} for i, (_, c, v) in self._offers.items()]
        return {"offers": sorted(offers, key=lambda o: o["device"]), "running": self.running,
                "progress": self.progress, "last": self.last}

    async def install(self, dev_id: str) -> None:
        """Installe le firmware proposé pour `dev_id` (LookupError s'il n'y en a pas)."""
        if self.running:
            raise RuntimeError(f"mise à jour de {self.running} en cours")
        device, _, version = self._offers[dev_id]
        self.running, self.progress, self.last = dev_id, 0, ""
        self.on_change()
        log.info("%s : installation du firmware %s", dev_id, version)
        try:
            await device.update_firmware(self._advance)
        except DeviceError as err:
            self.last = f"Échec : {err}"
            log.warning("%s : firmware non installé (%s)", dev_id, err)
        except Exception:
            self.last = f"Échec : {dev_id} : erreur inattendue, voir le journal."
            log.exception("%s : installation du firmware en erreur", dev_id)
        else:
            self.last = f"{dev_id} : firmware {version} installé."
            log.info("%s : firmware %s installé", dev_id, version)
            self._offers.pop(dev_id, None)
        finally:
            self.running = None
            self.on_change()

    def _advance(self, percent: float) -> None:
        if int(percent) != self.progress:  # un message par pourcent, pas par bloc envoyé
            self.progress = int(percent)
            self.on_change()


updates = Firmwares()
