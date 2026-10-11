"""Automatisations : fonctions déclenchées par les changements d'état des équipements.

Un fichier par sujet dans automations/, chargé au démarrage (comme drivers/). Une seule ligne
d'import suffit :

    from core.automation import devices, log, on_change

    @on_change("fp2.presence_0")              # « équipement.attribut » ; « fp2 » seul = tout l'état
    async def salon(event):                   # event.old, event.new, event.changed
        await devices["salon"].set("power", "on" if event.new else "off")

`to=` ne déclenche que vers une valeur donnée : @on_change("fp2.presence_0", to=True).
Un attribut absent de l'état (équipement injoignable) n'est pas un changement : on garde la
dernière valeur connue, et rien ne se déclenche tant que la valeur revient identique.
Les commandes sont envoyées sans se soucier de l'état de l'équipement : s'il est injoignable,
c'est journalisé et le script continue (set() renvoie alors False). Les événements d'une même
fonction sont traités dans l'ordre ; une erreur dans une automatisation n'arrête pas les autres.
"""

import asyncio
import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from core import ROOT
from core.base import Device, DeviceError

log = logging.getLogger(__name__)  # partagé : les scripts l'importent aussi
__all__ = ["devices", "log", "on_change"]

AUTOMATIONS_DIR = ROOT / "automations"
_ANY = object()  # « to » non précisé (None et False sont des valeurs valides)


class _Commands:
    """Équipement vu par les automatisations : une commande vers un équipement injoignable
    est journalisée, jamais levée. Le reste (state(), id, name...) est celui de l'équipement."""

    def __init__(self, device: Device):
        self._device = device

    async def set(self, name: str, value: str | int, transition: int | None = None) -> bool:
        try:
            await self._device.set(name, value, transition)
        except DeviceError as err:
            log.warning("%s : commande %s=%s non envoyée (%s)", self._device.id, name, value, err)
            return False
        return True

    def __getattr__(self, attr: str):
        return getattr(self._device, attr)


devices: dict[str, _Commands] = {}  # rempli au démarrage : devices["salon"].set(...)


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
    devices.update({dev_id: _Commands(dev) for dev_id, dev in devs.items()})
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
