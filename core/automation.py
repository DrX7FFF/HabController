"""Moteur des automatisations de Config/automations : chargement et minuteurs. Les
automatisations s'abonnent aux événements des équipements (@equipement.evt_…, core.base)."""

import asyncio
import importlib
import logging
from collections.abc import Callable
from typing import Any

from core import CONFIG_DIR
from core.base import DeviceError, clear_listeners, remove_listeners, spawn_task

log = logging.getLogger(__name__)
__all__ = ["Timer"]

AUTOMATIONS_DIR = CONFIG_DIR / "automations"
_running: set[asyncio.Task] = set()  # minuteurs en cours, annulés à unload


def _spawn_task(coro, name: str) -> asyncio.Task:
    task = spawn_task(coro, name)
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task


class Timer:
    """Minuteur réarmable : start() (re)lance le décompte, cancel() l'arrête. À l'échéance,
    action() est appelée (fonction simple ou async). Annulé au rechargement, comme les
    automatisations en cours."""

    def __init__(self, delay: float, action: Callable[[], Any]):
        self.delay = delay  # s
        self.action = action
        self.name = f"{action.__module__.rpartition('.')[2]}.{action.__name__}"
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        self.cancel()
        self._task = _spawn_task(self._wait(), self.name)

    def cancel(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _wait(self) -> None:
        await asyncio.sleep(self.delay)
        try:
            result = self.action()
            if asyncio.iscoroutine(result):
                await result
        except (DeviceError, ValueError) as err:
            log.warning("%s : %s : %s", self.name, type(err).__name__, err)


def load_automations() -> None:
    """Charge automations/*.py (sauf les fichiers commençant par _). Un fichier en erreur est
    ignoré en entier : ses abonnements déjà faits sont retirés."""
    for path in sorted(AUTOMATIONS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = f"automations.{path.stem}"
        try:
            importlib.import_module(module)
        except Exception:
            remove_listeners(module)
            log.exception("automations/%s : fichier ignoré", path.name)


async def unload_automations() -> None:
    """Retire les abonnements des automatisations, annule celles en cours et les minuteurs. Les
    commandes déjà envoyées aux équipements vont à leur terme : les équipements restent en
    marche (rechargement des seules automatisations)."""
    await clear_listeners()
    while running := [task for task in _running if not task.done()]:  # voir base.cancel_tasks
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
