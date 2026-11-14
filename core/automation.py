"""Moteur des automatisations de Config/automations : chargement et minuteurs. Les
automatisations s'abonnent aux événements des équipements (@equipement.event_…, core.base)
et de l'application (@app_start, @app_reload) ; variables gardées au rechargement : KeepOnReload.

Un script n'importe que ce module : `from core.automation import *` (voir __all__)."""

import asyncio
import importlib
import inspect
import logging
import sys
from collections.abc import Callable
from typing import Annotated, Any

from core import CONFIG_DIR
from core.base import (
    DeviceError,
    Event,
    EventAction,
    EventBool,
    EventFloat,
    EventInt,
    EventStr,
    EventZone,
    clear_listeners,
    listeners,
    remove_listeners,
    spawn_task,
)

_log = logging.getLogger(__name__)  # moteur : system.log
log = logging.getLogger("automations")  # scripts : automations.log, avec le nom du fichier
__all__ = [
    "Event",
    "EventAction",
    "EventBool",
    "EventFloat",
    "EventInt",
    "EventStr",
    "EventZone",
    "KeepOnReload",
    "Timer",
    "app_reload",
    "app_start",
    "log",
]

AUTOMATIONS_DIR = CONFIG_DIR / "automations"
_running: set[asyncio.Task] = set()  # minuteurs en cours, annulés à unload

# Annotation d'une variable de script (`presence: KeepOnReload = None`) : sa valeur est reprise au
# rechargement des automatisations ; au démarrage, la valeur écrite dans le script.
KeepOnReload = Annotated[Any, "KeepOnReload"]
_kept: dict[str, dict[str, Any]] = {}  # module -> variable -> valeur, jusqu'au rechargement
_app: dict[str, list[Callable]] = {"start": [], "reload": []}  # événements de l'application


def app_start(func: Callable) -> Callable:
    """@app_start : func() (async) au démarrage, automatisations chargées, avant la connexion
    des équipements. Aussi au rechargement de devices/ (équipements redémarrés)."""
    _app["start"].append(func)
    return func


def app_reload(func: Callable) -> Callable:
    """@app_reload : func() (async) au rechargement des seules automatisations, variables
    KeepOnReload reprises."""
    _app["reload"].append(func)
    return func


async def fire_app(name: str) -> None:
    """Déclenche @app_start ou @app_reload, une fonction après l'autre."""
    for func in list(_app[name]):
        try:
            await func()
        except Exception:
            _log.exception("%s.%s : erreur", func.__module__.rpartition(".")[2], func.__name__)


def _kept_names(module) -> list[str]:
    try:
        annotations = inspect.get_annotations(module)
    except Exception:  # annotation invalide (évaluée à la demande en Python 3.14)
        return []
    return [name for name, ann in annotations.items() if ann in (KeepOnReload, "KeepOnReload")]


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
            _log.warning("%s : %s : %s", self.name, type(err).__name__, err)


def check() -> None:
    """Signale les event_ d'attribut qui ne se déclencheront jamais (voir Device.check_measure) ;
    event_zone et event_action ne sont pas contrôlés."""
    for device, key, func in listeners():
        if key in ("zone", "action"):
            continue
        if reason := device.check_measure(key):
            _log.warning(
                "%s.%s : %s.event_%s : %s, jamais déclenchée",
                func.__module__.rpartition(".")[2], func.__name__, device.id, key, reason,
            )


def load_automations(reload: bool = False) -> None:
    """Charge automations/*.py (sauf les fichiers commençant par _). Un fichier en erreur est
    ignoré en entier : ses abonnements déjà faits sont retirés. reload : variables KeepOnReload
    reprises ; un fichier en erreur garde les siennes pour le prochain rechargement, celles
    d'un fichier supprimé sont oubliées."""
    global _kept
    paths = [p for p in sorted(AUTOMATIONS_DIR.glob("*.py")) if not p.name.startswith("_")]
    modules = {f"automations.{p.stem}": p for p in paths}
    _kept = {m: v for m, v in _kept.items() if m in modules} if reload else {}
    for module, path in modules.items():
        try:
            mod = importlib.import_module(module)
        except Exception:
            remove_listeners(module)
            for funcs in _app.values():
                funcs[:] = [f for f in funcs if f.__module__ != module]
            _log.exception("automations/%s : fichier ignoré", path.name)
            continue
        saved = _kept.pop(module, {})
        for name in _kept_names(mod):
            if name in saved:
                setattr(mod, name, saved[name])


async def unload_automations() -> None:
    """Retire les abonnements des automatisations, annule celles en cours et les minuteurs. Les
    commandes déjà envoyées aux équipements vont à leur terme : les équipements restent en
    marche (rechargement des seules automatisations). Les variables KeepOnReload sont relevées."""
    for name, mod in list(sys.modules.items()):
        if name.startswith("automations.") and mod is not None:
            _kept[name] = {v: getattr(mod, v) for v in _kept_names(mod) if hasattr(mod, v)}
    for funcs in _app.values():
        funcs.clear()
    await clear_listeners()
    while running := [task for task in _running if not task.done()]:  # voir base.cancel_tasks
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
