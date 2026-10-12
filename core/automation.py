"""Automatisations : fonctions déclenchées par les changements d'état des équipements.

Un fichier par sujet dans automations/, chargé au démarrage (comme drivers/). Les équipements
s'importent par leur identifiant depuis config/devices/, avec le déclencheur et le journal :

    from core.automation import capteur, cuisine, log, on_change, salon

    @on_change("capteur.presence")            # « équipement.attribut » ; « capteur » = tout l'état
    async def eclairage(event):               # event.old, event.new, event.changed
        if capteur.reachable and capteur.presence:
            salon.power = "on"                # écriture : commande envoyée en tâche de fond
            cuisine.power = "on"

Lecture : un attribut vaut None si l'équipement est injoignable (l'attribut disparaît de son
état) ou si le nom n'existe pas : à tester avant de l'utiliser. Écriture : salon.power = "on" ne
bloque pas et ne lève rien si l'équipement est injoignable (c'est journalisé) ; les commandes
d'un même équipement partent dans l'ordre. Pour attendre la réponse, ou passer une transition :
`ok = await salon.set("power", "on", 2)`, qui renvoie False si la commande n'a pas pu partir.
Un nom d'équipement inconnu est signalé au démarrage.

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
from core.base import Device, DeviceError

log = logging.getLogger(__name__)  # partagé : les scripts l'importent aussi
__all__ = ["devices", "log", "on_change"]

AUTOMATIONS_DIR = ROOT / "automations"
_ANY = object()  # « to » non précisé (None et False sont des valeurs valides)


class Equipment:
    """Un équipement vu par les automatisations : fp2.illuminance lit son état mémorisé,
    salon.power = "on" lui envoie une commande (voir l'en-tête du module)."""

    __slots__ = ("_device", "_id", "_lock")

    def __init__(self, dev_id: str):
        self._id = dev_id
        self._device: Device | None = None  # renseigné par bind()
        self._lock = asyncio.Lock()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return None if self._device is None else self._device.get_attribute(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        _spawn_task(self._send(name, value), f"{self._id}.{name}")

    async def set(self, name: str, value: str | int, transition: int | None = None) -> bool:
        """Envoie la commande et attend la réponse ; False si elle n'a pas pu partir."""
        if self._device is None:
            log.warning("%s : équipement inconnu, commande %s=%s ignorée", self._id, name, value)
            return False
        async with self._lock:  # les commandes d'un même équipement partent dans l'ordre
            try:
                await self._device.set(name, value, transition)
            except DeviceError as err:
                log.warning("%s : commande %s=%s non envoyée (%s)", self._id, name, value, err)
                return False
        return True

    async def _send(self, name: str, value: Any) -> None:
        try:
            await self.set(name, value)
        except ValueError as err:  # commande ou valeur refusée par le pilote : erreur du script
            log.warning("%s : commande %s=%r refusée (%s)", self._id, name, value, err)


_equipments: dict[str, Equipment] = {}  # un objet unique par nom, créé à l'import des scripts
devices: dict[str, Equipment] = {}  # équipements existants, par identifiant : devices["salon"]


def _equipment(dev_id: str) -> Equipment:
    if dev_id not in _equipments:
        _equipments[dev_id] = Equipment(dev_id)
    return _equipments[dev_id]


def __getattr__(name: str) -> Equipment:
    """`from core.automation import salon` : tout nom d'équipement s'importe (vérifié par bind)."""
    if name.startswith("_"):
        raise AttributeError(name)
    return _equipment(name)


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


def _spawn_task(coro, name: str) -> None:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_done)


def _spawn(handler: Handler, event: Event) -> None:
    _spawn_task(_run(handler, event), handler.name)


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
    for equipment in _equipments.values():
        equipment._device = None
        equipment._lock = asyncio.Lock()
    for dev_id, dev in devs.items():
        devices[dev_id] = _equipment(dev_id)
        devices[dev_id]._device = dev
    for dev_id, equipment in _equipments.items():
        if equipment._device is None:
            log.error("équipement inconnu « %s » utilisé dans les automatisations", dev_id)
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
