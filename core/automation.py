"""Moteur des automatisations de Config/automations."""

import asyncio
import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, TypeVar

from core import CONFIG_DIR
from core.base import Device, DeviceError, spawn_task

log = logging.getLogger(__name__)
__all__ = ["Event", "Timer", "on_change", "on_event"]

AUTOMATIONS_DIR = CONFIG_DIR / "automations"
_ANY = object()  # « to » non précisé (None et False sont des valeurs valides)


@dataclass(frozen=True)
class Event:
    device: str
    attr: (
        str | None
    )  # None : automatisation sur tout l'équipement (old/new = états complets) ;
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
_bindings: list[tuple[Device, Callable]] = []  # abonnements aux équipements, retirés à unload
_running: set[asyncio.Task] = set()  # automatisations et minuteurs en cours, annulés à unload
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
        raise ValueError(
            f"cible invalide « {target} » (attendu « équipement.attribut »)"
        )

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
        self.last: dict = dict(
            device.state()
        )  # dernières valeurs connues, par attribut

    def on_state(self, state: dict) -> None:
        changed = frozenset(
            a for a, v in state.items() if a not in self.last or self.last[a] != v
        )
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
                event = Event(
                    self.dev_id, handler.attr, old.get(handler.attr), new, changed, when
                )
            else:
                continue
            _spawn(handler, event)


def _dispatch_event(
    device: Device, handlers: list[Handler], name: str, value: Any
) -> None:
    """Événement ponctuel de l'équipement : lance les @on_event concernés."""
    for handler in handlers:
        if handler.attr in (None, name):
            event = Event(
                device.id, name, None, value, frozenset({name}), device.last_seen or 0
            )
            _spawn(handler, event)


def _spawn_task(coro, name: str) -> asyncio.Task:
    task = spawn_task(coro, name)
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task


def _spawn(handler: Handler, event: Event) -> None:
    _spawn_task(_run(handler, event), handler.name)


async def _run(handler: Handler, event: Event) -> None:
    async with (
        handler.lock
    ):  # même fonction : un événement à la fois, dans l'ordre d'arrivée
        try:
            await handler.func(event)
        except (DeviceError, ValueError) as err:
            log.warning("%s : %s : %s", handler.name, type(err).__name__, err)


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


def bind(devs: dict[str, Device]) -> None:
    """Abonne les automatisations déclarées aux équipements (avant leur démarrage)."""
    by_device: dict[str, list[Handler]] = {}
    for handler in _handlers:
        if handler.device not in devs:
            log.error(
                "%s : équipement inconnu « %s », ignorée", handler.name, handler.device
            )
            continue
        by_device.setdefault(handler.device, []).append(handler)
    for dev_id, handlers in by_device.items():
        device = devs[dev_id]
        if changes := [h for h in handlers if not h.event]:
            callback = _Watcher(device, changes).on_state
            device.subscribe(callback)
            _bindings.append((device, callback))
        if events := [h for h in handlers if h.event]:
            callback = partial(_dispatch_event, device, events)
            device.subscribe_events(callback)
            _bindings.append((device, callback))


def check(devs: dict[str, Device]) -> None:
    """Signale les @on_change sur un attribut qui ne sera jamais reçu (voir
    Device.check_measure)."""
    for handler in _handlers:
        if handler.event or not handler.attr or handler.device not in devs:
            continue
        if reason := devs[handler.device].check_measure(handler.attr):
            log.warning(
                "%s : %s.%s : %s, jamais déclenchée",
                handler.name, handler.device, handler.attr, reason,
            )


def load_automations(devs: dict[str, Device]) -> None:
    """Charge automations/*.py (sauf les fichiers commençant par _) puis les abonne. Un fichier
    en erreur est ignoré en entier : ses automatisations déjà déclarées sont retirées."""
    for path in sorted(AUTOMATIONS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = f"automations.{path.stem}"
        try:
            importlib.import_module(module)
        except Exception:
            _handlers[:] = [h for h in _handlers if h.func.__module__ != module]
            log.exception("automations/%s : fichier ignoré", path.name)
    bind(devs)


async def unload_automations() -> None:
    """Désabonne les automatisations des équipements, annule celles en cours et les minuteurs,
    puis les oublie. Les commandes déjà envoyées aux équipements vont à leur terme : les
    équipements restent en marche (rechargement des seules automatisations)."""
    for device, callback in _bindings:
        device.unsubscribe(callback)
    _bindings.clear()
    _handlers.clear()
    while running := [task for task in _running if not task.done()]:  # voir base.cancel_tasks
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
