"""Moteur des automatisations de HabConfig/automations : chargement et minuteurs. Les
automatisations s'abonnent aux événements des équipements (@equipement.event_…, core.base)
et de l'application (@app_start, @app_reload) ; variables gardées au rechargement : KeepOnReload ;
notifications push sur le téléphone : notify, Question (core.push).

Un script n'importe que ce module : `from core.automation import *` (voir __all__)."""

import asyncio
import importlib
import inspect
import logging
import sys
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Annotated, Any

from core import CONFIG_DIR, push
from core.base import (
    DeviceError,
    Event,
    EventAction,
    EventBool,
    EventFloat,
    EventInt,
    EventStr,
    EventZone,
    cancel,
    clear_listeners,
    listeners,
    remove_listeners,
    spawn_task,
)
from core.push import Question, notify

_logger = logging.getLogger(__name__)  # moteur : system.log
log = logging.getLogger("automations")  # scripts : automations.log, avec le nom du fichier
__all__ = [
    "DeviceError",
    "Event",
    "EventAction",
    "EventBool",
    "EventFloat",
    "EventInt",
    "EventStr",
    "EventZone",
    "KeepOnReload",
    "Question",
    "Timer",
    "app_reload",
    "app_start",
    "log",
    "notify",
]

AUTOMATIONS_DIR = CONFIG_DIR / "automations"
_running: set[asyncio.Task[None]] = set()  # minuteurs en cours, annulés à unload

# Annotation d'une variable de script (`presence: KeepOnReload = None`) : sa valeur est reprise au
# rechargement des automatisations ; au démarrage, la valeur écrite dans le script.
KeepOnReload = Annotated[Any, "KeepOnReload"]
_kept: dict[str, dict[str, object]] = {}  # module -> variable -> valeur, jusqu'au rechargement
type AppHandler = Callable[[], Awaitable[object]]
_app: dict[str, list[AppHandler]] = {"start": [], "reload": []}  # événements de l'application


def app_start[F: AppHandler](func: F) -> F:
    """@app_start : func() (async) au démarrage, automatisations chargées, avant la connexion
    des équipements. Aussi au rechargement de devices/ (équipements redémarrés)."""
    _app["start"].append(func)
    return func


def app_reload[F: AppHandler](func: F) -> F:
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
            _logger.exception("%s.%s : erreur", func.__module__.rpartition(".")[2], func.__name__)


def _kept_names(module: ModuleType) -> list[str]:
    annotations = inspect.get_annotations(module)
    return [name for name, ann in annotations.items() if ann in (KeepOnReload, "KeepOnReload")]


class Timer:
    """Minuteur réarmable : start() (re)lance le décompte, cancel() l'arrête. À l'échéance,
    action() est appelée (fonction simple ou async). Annulé au rechargement, comme les
    automatisations en cours."""

    def __init__(self, delay: float, action: Callable[[], object]):
        self.delay = delay  # s
        self.action = action
        self.name = f"{action.__module__.rpartition('.')[2]}.{action.__name__}"
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        self.cancel()
        self._task = spawn_task(self._wait(), self.name, _running)

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
            _logger.warning("%s : %s : %s", self.name, type(err).__name__, err)


def check() -> None:
    """Signale les event_ d'attribut qui ne se déclencheront jamais (voir Device.check_measure) ;
    event_zone et event_action ne sont pas contrôlés."""
    for device, key, func in listeners():
        if key in ("zone", "action"):
            continue
        if reason := device.check_measure(key):
            _logger.warning(
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
            _logger.exception("automations/%s : fichier ignoré", path.name)
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
        if name.startswith("automations."):
            _kept[name] = {v: getattr(mod, v) for v in _kept_names(mod) if hasattr(mod, v)}
    for funcs in _app.values():
        funcs.clear()
    push.forget()  # questions en attente : leurs actions seraient celles des anciens scripts
    await clear_listeners()
    await cancel(_running)
