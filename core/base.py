import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from abc import ABC, abstractmethod
from functools import cache, partial
from typing import Any, Generic, TypeVar, overload

log = logging.getLogger(__name__)
T = TypeVar("T")
E = TypeVar("E")

tasks: set[asyncio.Task] = set()  # tâches de fond (commandes, automatisations), annulées à l'arrêt
_listeners: list[tuple["Device", str, Callable]] = []  # abonnements event_, retirés par clear_listeners
_listener_tasks: set[asyncio.Task] = set()  # fonctions abonnées en cours


def _done(task: asyncio.Task) -> None:
    tasks.discard(task)
    if not task.cancelled() and (err := task.exception()):
        log.error("%s : erreur inattendue", task.get_name(), exc_info=err)


def spawn_task(coro, name: str) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    tasks.add(task)
    task.add_done_callback(_done)
    return task


async def cancel_tasks() -> None:
    """Annule toutes les tâches de fond en cours (arrêt ou rechargement complet)."""
    # Une tâche annulée peut en avoir lancé d'autres. On ne suit que les tâches en cours : une
    # tâche finie reste dans tasks jusqu'à son rappel _done, et gather() sur des tâches toutes
    # finies rend la main sans passer par la boucle, qui tournerait alors à vide.
    while running := [task for task in tasks if not task.done()]:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


class DeviceError(Exception):
    """Équipement injoignable ou commande refusée."""


@dataclass(frozen=True)
class Event(Generic[T]):
    """Attribut changé : première valeur reçue (old None) ou valeur différente de la précédente."""

    device: "Device"
    time: int  # ms depuis l'époque Unix
    old: T | None
    new: T


EventBool = Event[bool]
EventInt = Event[int]
EventFloat = Event[float]
EventStr = Event[str]


@dataclass(frozen=True)
class EventZone:
    """Présence d'une zone changée (détecteur à zones)."""

    device: "Device"
    time: int  # ms depuis l'époque Unix
    zonename: str
    old: bool | None
    new: bool


@dataclass(frozen=True)
class EventAction:
    """Action ponctuelle de l'équipement (appui de bouton…), avec sa valeur éventuelle."""

    device: "Device"
    time: int  # ms depuis l'époque Unix
    action: str
    value: Any


Listener = Callable[[E], Awaitable[None]]


class EventSource(Generic[E]):
    """Événement d'un équipement, déclaré par son pilote : `@equipement.event_x` abonne une
    fonction async(event: E). Clé : le nom sans « event_ » (attribut d'état, « zone » ou « action »)."""

    def __init__(self):
        self.key = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self.key = name.removeprefix("event_")

    @overload
    def __get__(self, obj: None, owner: type) -> "EventSource[E]": ...
    @overload
    def __get__(self, obj: "Device", owner: type) -> Callable[[Listener[E]], Listener[E]]: ...
    def __get__(self, obj, owner=None) -> Any:
        if obj is None:
            return self
        return partial(obj._listen, self.key)


def listeners() -> list[tuple["Device", str, Callable]]:
    """Abonnements evt_ en cours : (équipement, clé d'EventSource, fonction)."""
    return list(_listeners)


def remove_listeners(module: str) -> None:
    """Retire les abonnements event_ faits par les fonctions de `module`."""
    for device, key, func in [item for item in _listeners if item[2].__module__ == module]:
        device._evt_listeners[key].remove(func)
        _listeners.remove((device, key, func))


async def clear_listeners() -> None:
    """Retire tous les abonnements event_ et annule les fonctions abonnées en cours."""
    for device, key, func in _listeners:
        device._evt_listeners[key].remove(func)
    _listeners.clear()
    while running := [task for task in _listener_tasks if not task.done()]:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


def on_value(value: bool | str | int) -> bool:
    """Valeur demandée pour l'attribut `on` : True ou False."""
    if isinstance(value, bool):
        return value
    raise ValueError("on attend True ou False")


class State(Generic[T]):
    """Attribut d'état : la valeur est gardée dans l'équipement, en lecture seule
    (`cuisine.on`) ; seul le pilote l'écrit, par `Light.on.update(self, valeur)`, qui annonce
    le changement (événement event_<nom>, pages)."""

    def __init__(self, default: T | None = None):
        self.name = ""
        self.default = default  # valeur tant que le pilote n'a rien écrit

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    @overload
    def __get__(self, obj: None, owner: type) -> "State[T]": ...
    @overload
    def __get__(self, obj: "Device", owner: type) -> T | None: ...
    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        return obj.__dict__.get(self.name, self.default)

    def __set__(self, obj: "Device", value: T) -> None:
        raise AttributeError(f"{self.name} est en lecture seule (utiliser les méthodes)")

    def update(self, obj: "Device", value: T | None) -> None:
        old = obj.__dict__.get(self.name, self.default)
        obj.__dict__[self.name] = value
        obj._updated(self.name, old, value)


class Capability(State[T]):
    """Ce que le modèle sait faire (rgb_capable…) : renseigné par le pilote, gardé hors
    connexion."""


@cache
def states(cls: type) -> tuple[State, ...]:
    """Attributs d'état de la classe et de ses parents, dans l'ordre de déclaration."""
    return tuple(s for c in reversed(cls.__mro__) for s in vars(c).values() if isinstance(s, State))


# État de surveillance d'une mesure (voir Device.measures) -> pourquoi elle ne sera jamais reçue
REASONS = {
    "tracked": "",
    "disabled": "non suivie (reports)",
    "absent": "l'appareil ne la fournit pas",
}


class Device(ABC):
    """Base commune des pilotes : identité, état et commandes."""

    WIDGET = ""  # widget du tableau de bord : sensor, plug, onoff ou light ; "" : aucun
    connected = State[bool]()  # joignable (et état lu) ; None : sans connexion suivie
    battery_powered = Capability[bool](default=False)  # sur pile, renseigné par le pilote
    battery = State[int]()  # %, équipement sur pile
    event_connected = EventSource[EventBool]()
    event_battery = EventSource[EventInt]()

    def __init__(self):
        self.id = ""  # affecté par configure() : le chargeur le déduit du nom de la variable
        self._listeners: list[Callable[[dict], None]] = []
        self._ready_listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()  # les commandes d'un même équipement partent dans l'ordre
        self._last: dict = {}  # dernière valeur annoncée par attribut, gardée hors connexion
        self.changed_at: int | None = None  # ms depuis l'époque Unix, dernier changement d'état
        self.last_seen: int | None = None  # ms, dernière mise à jour reçue (même identique)
        self._evt_listeners: dict[str, list[Callable]] = {}  # clé d'EventSource -> fonctions abonnées
        self._evt_lock = asyncio.Lock()  # fonctions abonnées : une à la fois, dans l'ordre

    def _reset(self) -> None:
        """Équipement injoignable : attributs d'état à leur valeur par défaut, sans annonce
        (connected et capacités gardés)."""
        for state in states(type(self)):
            if state is not Device.connected and not isinstance(state, Capability):
                self.__dict__.pop(state.name, None)

    def configure(self, dev_id: str) -> None:
        """Appelé par le chargeur, avant start() : identifiant."""
        self.id = dev_id

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        """callback(state) est appelé à chaque changement d'état de l'équipement."""
        self._listeners.append(callback)

    def subscribe_ready(self, callback: Callable[[], None]) -> None:
        """callback() est appelé quand l'équipement devient prêt ou cesse de l'être (voir
        ready)."""
        self._ready_listeners.append(callback)

    def unsubscribe(self, callback: Callable) -> None:
        """Retire un abonnement de subscribe() ou subscribe_ready()
        (rechargement partiel)."""
        for listeners in (self._listeners, self._ready_listeners):
            if callback in listeners:
                listeners.remove(callback)

    def _emit(self, name: str, value: Any = None) -> None:
        """Événement ponctuel (bouton : single, double, hold…) : rien dans l'état, transmis tel
        quel avec sa valeur éventuelle (ex. angle de rotation)."""
        self.last_seen = time.time_ns() // 1_000_000
        self._fire("action", EventAction(self, self.last_seen, name, value))

    def _updated(self, name: str, old: Any, new: Any, announce: Callable | None = None) -> None:
        """Appelé par State.update : valeur reçue (last_seen) ; si elle change, l'état est
        diffusé (sauf injoignable : il le sera à la connexion), et l'événement déclenché si elle
        diffère de la dernière annoncée. announce(when, old, new) : remplace l'événement Event
        de l'attribut."""
        now = time.time_ns() // 1_000_000
        if self.connected:
            self.last_seen = now
        if new == old:
            return
        self.changed_at = now
        if self.connected is not False or name == "connected":
            self._broadcast()
        if new is not None and (name not in self._last or self._last[name] != new):
            if announce:
                announce(now, self._last.get(name), new)
            else:
                self._fire(name, Event(self, now, self._last.get(name), new))
            self._last[name] = new

    def _seen(self) -> None:
        """Message reçu sans valeur changée."""
        if self.connected:
            self.last_seen = time.time_ns() // 1_000_000

    def _broadcast(self) -> None:
        state = self.state()
        for callback in self._listeners:
            callback(state)

    def _listen(self, key: str, func: Listener) -> Listener:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"{func.__name__} doit être une fonction async")
        self._evt_listeners.setdefault(key, []).append(func)
        _listeners.append((self, key, func))
        return func

    def _fire(self, key: str, event: Any) -> None:
        for func in self._evt_listeners.get(key, ()):
            name = f"{func.__module__.rpartition('.')[2]}.{func.__name__}"
            task = spawn_task(self._run_listener(func, event, name), name)
            _listener_tasks.add(task)
            task.add_done_callback(_listener_tasks.discard)

    async def _run_listener(self, func: Listener, event: Any, name: str) -> None:
        async with self._evt_lock:  # les tâches démarrent dans l'ordre de création
            try:
                await func(event)
            except (DeviceError, ValueError) as err:
                log.warning("%s : %s : %s", name, type(err).__name__, err)

    def _ready_changed(self) -> None:
        """À appeler quand ready() change."""
        for callback in self._ready_listeners:
            callback()

    async def start(self) -> None:
        """Appelé au démarrage de l'application (connexion, écoute...)."""

    async def stop(self) -> None:
        """Appelé à l'arrêt de l'application."""

    def physical(self) -> str:
        """Appareil physique lié (protocole et identité), "" s'il n'y en a pas."""
        return ""

    def details(self) -> dict:
        """Fiche de l'appareil physique lié, d'après ce que l'appairage en a retenu (sans
        l'interroger) : {libellé: texte, ou fiche imbriquée}."""
        return {"physical": self.physical()}

    def ready(self) -> bool:
        """Appareil connu : l'état de surveillance de ses mesures est définitif (voir
        measures)."""
        return True

    def measures(self) -> dict[str, str]:
        """État de surveillance de chaque attribut : tracked (peut être reçu), disabled (non
        suivi, voir reports), absent (l'appareil ne le fournit pas)."""
        return {
            state.name: "tracked"
            for state in states(type(self))
            if not isinstance(state, Capability)
            and (state is not Device.battery or self.battery_powered)
        }

    def check_measure(self, name: str) -> str:
        """Pourquoi la mesure `name` ne sera jamais reçue ("" si elle peut l'être), d'après son
        état de surveillance."""
        return REASONS.get(self.measures().get(name, ""), "mesure inconnue")

    def state(self) -> dict:
        """État envoyé aux pages et au datalog : les attributs connus (None : omis), sans les
        capacités."""
        return {
            state.name: value
            for state in states(type(self))
            if not isinstance(state, Capability)
            and (value := state.__get__(self, type(self))) is not None
        }

    def _background(self, label: str, command: Callable[[], Awaitable[None]]) -> None:
        """Lance command() en tâche de fond, après les commandes précédentes de l'équipement.

        Équipement injoignable ou commande refusée : journalisé, rien ne remonte au script.
        """
        spawn_task(self._background_task(label, command), f"{self.id}.{label}")

    async def _background_task(self, label: str, command: Callable[[], Awaitable[None]]) -> None:
        async with self._lock:
            try:
                await command()
            except DeviceError as err:
                log.warning("%s : commande %s non envoyée (%s)", self.id, label, err)
            except ValueError as err:
                log.warning("%s : commande %s refusée (%s)", self.id, label, err)


class NetworkDevice(Device):
    """Base pour les équipements avec connexion réseau (HomeKit, WiFi, etc.)."""

    def __init__(self):
        super().__init__()
        self._connected = False  # liaison établie (connected : liaison établie et état lu)
        Device.connected.update(self, False)  # avant tout abonnement : rien n'est annoncé
        self._tasks: set[asyncio.Task] = set()  # tâches du pilote, annulées par stop()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _spawn(self, coro) -> asyncio.Task:
        """Tâche du pilote (connexion, lecture…) : annulée par stop(), erreur journalisée."""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (err := task.exception()):
            log.error("%s : erreur inattendue", self.id, exc_info=err)

    def _set_connected(
        self, connected: bool, synced: bool = False, reason: str = "", silent: bool = False
    ) -> None:
        """Connexion établie ou perdue : journalisée si elle change (sauf `silent`), état
        diffusé. `synced` : état déjà lu (sinon à lire ensuite) ; `reason` : cause de la perte."""
        if connected != self._connected and not silent:
            if connected:
                log.info("%s : connecté", self.id)
            else:
                log.warning("%s : déconnecté%s", self.id, f" ({reason})" if reason else "")
        self._connected = connected
        if not (connected and synced):
            self._reset()
        Device.connected.update(self, connected and synced)

    def _check_reachable(self) -> None:
        """DeviceError si l'équipement n'est pas joignable (connecté et état lu)."""
        if not self.connected:
            raise DeviceError(f"{self.id} : injoignable")


class Switchable(Device):
    """Équipement à marche/arrêt. Le pilote met `on` à jour à la réception et fournit
    _switch()."""

    on = State[bool]()
    event_on = EventSource[EventBool]()

    def switch(self, on: bool, transition: int | None = None) -> None:
        self._background(f"switch({on!r})", lambda: self._switch(on_value(on), transition))

    def toggle(self, transition: int | None = None) -> None:
        self._background("toggle", lambda: self._switch(not self.on, transition))

    @abstractmethod
    async def _switch(self, on: bool, transition: int | None) -> None: ...
