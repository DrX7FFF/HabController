"""Automatisations : fonctions déclenchées par les changements d'état des équipements.

Un fichier par sujet dans automations/, chargé au démarrage (comme drivers/). Les équipements
s'importent depuis devices/, avec le déclencheur :

    import logging

    from core.automation import on_change
    from devices.capteur import capteur
    from devices.cuisine import cuisine
    from devices.salon import salon

    log = logging.getLogger(__name__)  # journal des scripts : data/logs/automations.log

    @on_change("capteur.presence")            # « équipement.attribut » ; « capteur » = tout l'état
    async def eclairage(event):               # event.old, event.new, event.changed
        if capteur.reachable and capteur.presence:
            salon.power = "on"                # écriture : commande envoyée en tâche de fond
            cuisine.power = "on"

Lecture : un attribut vaut None si l'équipement est injoignable (l'attribut disparaît de son
état). Écriture : salon.power = "on" ne bloque pas et ne lève rien si l'équipement est
injoignable (c'est journalisé) ; les commandes d'un même équipement partent dans l'ordre.
Pour attendre la réponse, ou passer une transition : `ok = await salon.send("power", "on", 2)`,
qui renvoie False si la commande n'a pas pu partir. Un équipement inconnu dans un @on_change
est signalé au démarrage.

`to=` ne déclenche que vers une valeur donnée : @on_change("capteur.presence", to=True).
Un attribut absent de l'état (équipement injoignable) n'est pas un changement : on garde la
dernière valeur connue, et rien ne se déclenche tant que la valeur revient identique.
Les événements d'une même fonction sont traités dans l'ordre ; une erreur dans une
automatisation n'arrête pas les autres.
"""

import asyncio
import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from core import ROOT
from core.base import Device, DeviceError, spawn_task, tasks as _tasks

log = logging.getLogger(__name__)
__all__ = ["on_change"]

AUTOMATIONS_DIR = ROOT / "automations"
_ANY = object()  # « to » non précisé (None et False sont des valeurs valides)


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
    spawn_task(_run(handler, event), handler.name)


async def _run(handler: Handler, event: Event) -> None:
    async with handler.lock:  # même fonction : un événement à la fois, dans l'ordre d'arrivée
        try:
            await handler.func(event)
        except (DeviceError, ValueError) as err:
            log.warning("%s : %s : %s", handler.name, type(err).__name__, err)


def bind(devs: dict[str, Device]) -> None:
    """Abonne les automatisations déclarées aux équipements (avant leur démarrage)."""
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
    while _tasks:  # une tâche annulée peut en avoir lancé d'autres
        tasks = list(_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
