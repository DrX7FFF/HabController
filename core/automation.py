"""Automatisations : fonctions déclenchées par les changements d'état des équipements.

Un fichier par sujet dans automations/, chargé au démarrage (comme drivers/) :

    from core.automation import devices, on_change

    @on_change("fp2.presence_0")              # « équipement.attribut » ; « fp2 » seul = tout l'état
    async def salon(event):                   # event.old, event.new, event.changed
        await devices["salon"].set("power", "on" if event.new else "off")

`to=` ne déclenche que vers une valeur donnée : @on_change("fp2.presence_0", to=True).
Un attribut absent de l'état (équipement injoignable) n'est pas un changement : on garde la
dernière valeur connue, et rien ne se déclenche tant que la valeur revient identique.
Les événements d'une même fonction sont traités dans l'ordre. Une erreur d'équipement est
journalisée sans empêcher les événements suivants ni les autres automatisations.
"""

import asyncio
import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from core import ROOT
from core.base import Device, DeviceError

log = logging.getLogger(__name__)

AUTOMATIONS_DIR = ROOT / "automations"
_ANY = object()  # « to » non précisé (None et False sont des valeurs valides)

devices: dict[str, Device] = {}  # rempli au démarrage : devices["salon"].set(...)


@dataclass(frozen=True)
class Event:
    device: str
    attr: str | None  # None : automatisation sur tout l'équipement (old/new = états complets)
    old: Any  # dernière valeur connue (None si jamais vue)
    new: Any
    changed: frozenset[str]  # attributs de l'équipement modifiés par ce changement


@dataclass
class Handler:
    name: str
    device: str
    attr: str | None
    to: Any
    func: Callable[[Event], Awaitable[None]]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_handlers: list[Handler] = []
_tasks: set[asyncio.Task] = set()


def on_change(target: str, *, to: Any = _ANY):
    """Déclare une automatisation : target = « équipement » ou « équipement.attribut »."""
    device, _, attr = target.partition(".")
    if not device:
        raise ValueError(f"cible invalide « {target} » (attendu « équipement.attribut »)")

    def register(func):
        if not asyncio.iscoroutinefunction(func):
            raise TypeError(f"{func.__name__} doit être une fonction async")
        name = f"{func.__module__.rpartition('.')[2]}.{func.__name__}"
        _handlers.append(Handler(name, device, attr or None, to, func))
        return func

    return register


class _Watcher:
    """Suit l'état d'un équipement et lance les fonctions concernées à chaque changement."""

    def __init__(self, dev_id: str, handlers: list[Handler], state: dict):
        self.dev_id = dev_id
        self.handlers = handlers
        self.last: dict = dict(state)  # dernières valeurs connues, par attribut

    def on_state(self, state: dict) -> None:
        changed = frozenset(a for a, v in state.items() if a not in self.last or self.last[a] != v)
        if not changed:
            return
        old = dict(self.last)
        self.last.update(state)
        for handler in self.handlers:
            if handler.attr is None:
                event = Event(self.dev_id, None, old, dict(self.last), changed)
            elif handler.attr in changed:
                new = state[handler.attr]
                if handler.to is not _ANY and new != handler.to:
                    continue
                event = Event(self.dev_id, handler.attr, old.get(handler.attr), new, changed)
            else:
                continue
            _spawn(handler, event)


def _spawn(handler: Handler, event: Event) -> None:
    task = asyncio.get_running_loop().create_task(_run(handler, event), name=handler.name)
    _tasks.add(task)
    task.add_done_callback(_done)


async def _run(handler: Handler, event: Event) -> None:
    async with handler.lock:  # même fonction : un événement à la fois, dans l'ordre d'arrivée
        try:
            await handler.func(event)
        except (DeviceError, ValueError) as err:
            log.warning("%s : %s : %s", handler.name, type(err).__name__, err)


def _done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if not task.cancelled() and (err := task.exception()):
        log.error("%s : erreur inattendue", task.get_name(), exc_info=err)


def bind(devs: dict[str, Device]) -> None:
    """Abonne les automatisations déclarées aux équipements (avant leur démarrage)."""
    devices.clear()
    devices.update(devs)
    by_device: dict[str, list[Handler]] = {}
    for handler in _handlers:
        if handler.device not in devs:
            log.error("%s : équipement inconnu « %s », ignorée", handler.name, handler.device)
            continue
        by_device.setdefault(handler.device, []).append(handler)
    for dev_id, handlers in by_device.items():
        watcher = _Watcher(dev_id, handlers, devs[dev_id].state())
        devs[dev_id].subscribe(watcher.on_state)


def load_automations(devs: dict[str, Device]) -> None:
    """Charge automations/*.py (sauf les fichiers commençant par _) puis les abonne."""
    for path in sorted(AUTOMATIONS_DIR.glob("*.py")):
        if not path.name.startswith("_"):
            importlib.import_module(f"automations.{path.stem}")
    bind(devs)


async def stop_automations() -> None:
    for task in _tasks:
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
