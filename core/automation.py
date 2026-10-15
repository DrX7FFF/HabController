"""Automatisations : fonctions déclenchées par les changements d'état des équipements.

Un fichier par sujet dans automations/, chargé au démarrage. Les équipements
s'importent depuis le fichier de devices/ qui les déclare, avec le déclencheur :

    import logging

    from core.automation import Event, on_change
    from devices.salon import capteur, salon
    from devices.cuisine import cuisine

    log = logging.getLogger(__name__)  # journal des scripts : temp/automations.log

    @on_change("capteur.presence")            # « équipement.attribut » ; « capteur » = tout l'état
    async def eclairage(event: Event):        # event.old, event.new, event.changed, event.time
        if capteur.connected and capteur.presence:
            salon.on = True                   # écriture : commande envoyée en tâche de fond
            cuisine.on = True

Lecture : un attribut vaut None si l'équipement est injoignable (l'attribut disparaît de son
état). Écriture : salon.on = True ne bloque pas et ne lève rien si l'équipement est
injoignable (c'est journalisé) ; les commandes d'un même équipement partent dans l'ordre.
Pour attendre la réponse, ou passer une transition : `ok = await salon.send("on", True, 2)`,
qui renvoie False si la commande n'a pas pu partir. Un équipement inconnu dans un @on_change
est signalé au démarrage.

`to=` ne déclenche que vers une valeur donnée : @on_change("capteur.presence", to=True).

Événements ponctuels (boutons) : @on_event("bouton.double") à chaque double appui, ou
@on_event("bouton") pour tous ; event.attr est le nom de l'événement, event.new sa valeur
éventuelle (ex. angle de rotation du cube), event.old toujours None.
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
from functools import partial
from typing import Any, TypeVar

from core import CONFIG_DIR
from core.base import Device, DeviceError, spawn_task, tasks as _tasks

log = logging.getLogger(__name__)
__all__ = ["Event", "on_change", "on_event"]

AUTOMATIONS_DIR = CONFIG_DIR / "automations"
_ANY = object()  # « to » non précisé (None et False sont des valeurs valides)


@dataclass(frozen=True)
class Event:
    device: str
    attr: str | None  # None : automatisation sur tout l'équipement (old/new = états complets) ;
    # nom de l'événement pour @on_event
    old: Any  # dernière valeur connue (None si jamais vue)
    new: Any
    changed: frozenset[str]  # attributs de l'équipement modifiés par ce changement
    time: int  # horodatage du changement (ms depuis l'époque Unix), voir Device.changed_at


@dataclass
class Handler:
    name: str
    device: str
    attr: str | None
    to: Any
    func: Callable[[Event], Awaitable[None]]
    event: bool = False  # @on_event : événement ponctuel, pas changement d'état
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_handlers: list[Handler] = []
Rule = TypeVar("Rule", bound=Callable[[Event], Awaitable[None]])


def on_change(target: str, *, to: Any = _ANY) -> Callable[[Rule], Rule]:
    """Déclare une automatisation : target = « équipement » ou « équipement.attribut »."""
    return _register(target, to, event=False)


def on_event(target: str) -> Callable[[Rule], Rule]:
    """Déclare une automatisation sur un événement : « équipement » ou « équipement.nom »."""
    return _register(target, _ANY, event=True)


def _register(target: str, to: Any, event: bool) -> Callable[[Rule], Rule]:
    device, _, attr = target.partition(".")
    if not device:
        raise ValueError(f"cible invalide « {target} » (attendu « équipement.attribut »)")

    def register(func: Rule) -> Rule:
        if not asyncio.iscoroutinefunction(func):
            raise TypeError(f"{func.__name__} doit être une fonction async")
        name = f"{func.__module__.rpartition('.')[2]}.{func.__name__}"
        _handlers.append(Handler(name, device, attr or None, to, func, event))
        return func

    return register


class _Watcher:
    """Suit l'état d'un équipement et lance les fonctions concernées à chaque changement."""

    def __init__(self, device: Device, handlers: list[Handler]):
        self.device = device
        self.dev_id = device.id
        self.handlers = handlers
        self.last: dict = dict(device.state())  # dernières valeurs connues, par attribut

    def on_state(self, state: dict) -> None:
        changed = frozenset(a for a, v in state.items() if a not in self.last or self.last[a] != v)
        if not changed:
            return
        old = dict(self.last)
        self.last.update(state)
        when = self.device.changed_at or 0
        for handler in self.handlers:
            if handler.attr is None:
                event = Event(self.dev_id, None, old, dict(self.last), changed, when)
            elif handler.attr in changed:
                new = state[handler.attr]
                if handler.to is not _ANY and new != handler.to:
                    continue
                event = Event(self.dev_id, handler.attr, old.get(handler.attr), new, changed, when)
            else:
                continue
            _spawn(handler, event)


def _dispatch_event(device: Device, handlers: list[Handler], name: str, value: Any) -> None:
    """Événement ponctuel de l'équipement : lance les @on_event concernés."""
    for handler in handlers:
        if handler.attr in (None, name):
            event = Event(device.id, name, None, value, frozenset({name}), device.last_seen or 0)
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
        device = devs[dev_id]
        if changes := [h for h in handlers if not h.event]:
            device.subscribe(_Watcher(device, changes).on_state)
        if events := [h for h in handlers if h.event]:
            device.subscribe_events(partial(_dispatch_event, device, events))


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
