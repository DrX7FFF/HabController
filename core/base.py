import asyncio
import inspect
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from functools import cache, partial
from typing import ClassVar, cast, overload, override

log = logging.getLogger(__name__)

type Handler = Callable[..., Awaitable[None]]  # fonction abonnée à un événement
type Background = Coroutine[object, object, None]  # travail lancé en tâche de fond

tasks: set[asyncio.Task[None]] = set()  # tâches de fond (commandes, automatisations…)
_listeners: list[tuple["Device", str, Handler]] = []  # event_ : (équipement, clé, fonction)
_listener_tasks: set[asyncio.Task[None]] = set()  # fonctions abonnées en cours


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def describe(err: Exception) -> str:
    """Texte d'une erreur pour le journal : son message, à défaut son type."""
    return str(err) or type(err).__name__


def spawn_task(
    coro: Background, name: str, group: set[asyncio.Task[None]] | None = None
) -> asyncio.Task[None]:
    """Tâche de fond (dans tasks, et dans `group`) ; erreur inattendue journalisée."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    for owner in (tasks, group) if group is not None else (tasks,):
        owner.add(task)
        task.add_done_callback(owner.discard)
    task.add_done_callback(_done)
    return task


def _done(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (err := task.exception()):
        log.error("%s : erreur inattendue", task.get_name(), exc_info=err)


async def cancel(group: set[asyncio.Task[None]] = tasks) -> None:
    """Annule les tâches de `group` en cours, y compris celles qu'elles lancent en s'arrêtant."""
    # Seulement celles en cours : gather() sur des tâches finies rend la main sans passer par la
    # boucle, qui tournerait alors à vide.
    while running := [task for task in group if not task.done()]:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


class DeviceError(Exception):
    """Équipement injoignable ou commande refusée."""


@dataclass(frozen=True)
class Event[T]:
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
    value: object


type Listener[E] = Callable[[E], Awaitable[None]]


class EventSource[E]:
    """Événement d'un équipement, déclaré par son pilote : `@equipement.event_x` abonne une
    fonction async(event: E). Clé : le nom sans « event_ » (attribut d'état, « zone » ou
    « action »)."""

    def __init__(self):
        self.key = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self.key = name.removeprefix("event_")

    @overload
    def __get__(self, obj: None, owner: type) -> "EventSource[E]": ...
    @overload
    def __get__(self, obj: "Device", owner: type) -> Callable[[Listener[E]], Listener[E]]: ...
    def __get__(
        self, obj: "Device | None", owner: type | None = None
    ) -> "EventSource[E] | Callable[[Listener[E]], Listener[E]]":
        if obj is None:
            return self
        return partial(obj._listen, self.key)


def listeners() -> list[tuple["Device", str, Handler]]:
    """Abonnements event_ en cours : (équipement, clé d'EventSource, fonction)."""
    return list(_listeners)


def remove_listeners(module: str) -> None:
    """Retire les abonnements event_ faits par les fonctions de `module`."""
    _listeners[:] = [item for item in _listeners if item[2].__module__ != module]


async def clear_listeners() -> None:
    """Retire tous les abonnements event_ et annule les fonctions abonnées en cours."""
    _listeners.clear()
    await cancel(_listener_tasks)


def on_value(value: object) -> bool:
    """Valeur demandée pour l'attribut `on` (venue d'une page : tout type) : True ou False."""
    if isinstance(value, bool):
        return value
    raise ValueError("on attend True ou False")


class State[T]:
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
    def __get__(self, obj: "Device | None", owner: type | None = None) -> "State[T] | T | None":
        if obj is None:
            return self
        return vars(obj).get(self.name, self.default)

    def __set__(self, obj: "Device", value: T) -> None:
        raise AttributeError(f"{self.name} est en lecture seule (utiliser les méthodes)")

    def update(self, obj: "Device", value: T | None) -> None:
        old = vars(obj).get(self.name, self.default)
        vars(obj)[self.name] = value
        obj._updated(self.name, old, value)


class Capability[T](State[T]):
    """Ce que le modèle sait faire (rgb_capable…) : renseigné par le pilote, gardé hors
    connexion."""


@cache
def states(cls: type) -> "tuple[State[object], ...]":
    """Attributs d'état de la classe et de ses parents, dans l'ordre de déclaration."""
    found = (s for c in reversed(cls.__mro__) for s in vars(c).values() if isinstance(s, State))
    return tuple(cast("State[object]", s) for s in found)


# État de surveillance d'une mesure (voir Device.measures) -> pourquoi elle ne sera jamais reçue
REASONS = {
    "tracked": "",
    "disabled": "non suivie (reports)",
    "absent": "l'appareil ne la fournit pas",
}


def actionweb[F: Callable[..., None]](func: F) -> F:
    """Méthode appelable par les pages ; arguments vérifiés strictement à la réception."""
    vars(func)["actionweb"] = True
    return func


class Device:
    """Base commune des pilotes : identité, état et commandes."""

    WIDGET = ""  # widget de la page Commandes (web/pages/dashboard.js) ; "" : aucun
    ACTIONS: ClassVar[frozenset[str]] = frozenset()  # méthodes @actionweb, relevées à la création
    connected = State[bool]()  # joignable (et état lu) ; None : sans connexion suivie
    event_connected = EventSource[EventBool]()

    def __init__(self):
        self.id = ""  # affecté par configure() : le chargeur le déduit du nom de la variable
        self._listeners: list[Callable[[dict[str, object]], None]] = []
        self._ready_listeners: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()  # les commandes d'un même équipement partent dans l'ordre
        self._last: dict[str, object] = {}  # dernière valeur annoncée, gardée hors connexion
        self.changed_at: int | None = None  # ms depuis l'époque Unix, dernier changement d'état
        self._event_lock = asyncio.Lock()  # fonctions abonnées : une à la fois, dans l'ordre
        self._tasks: set[asyncio.Task[None]] = set()  # tâches du pilote, annulées par stop()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.ACTIONS = frozenset(
            name for name in dir(cls)
            if getattr(inspect.getattr_static(cls, name), "actionweb", False)
        )

    def _reset(self) -> None:
        """Équipement injoignable : attributs d'état à leur valeur par défaut, sans annonce
        (connected et capacités gardés)."""
        for state in states(type(self)):
            if state is not Device.connected and not isinstance(state, Capability):
                vars(self).pop(state.name, None)

    def configure(self, dev_id: str) -> None:
        """Appelé par le chargeur, avant start() : identifiant."""
        self.id = dev_id

    def subscribe(self, callback: Callable[[dict[str, object]], None]) -> None:
        """callback(state) est appelé à chaque changement d'état de l'équipement."""
        self._listeners.append(callback)

    def subscribe_ready(self, callback: Callable[[], None]) -> None:
        """callback() est appelé quand l'équipement devient prêt ou cesse de l'être (voir
        ready)."""
        self._ready_listeners.append(callback)

    def unsubscribe(self, callback: Callable[..., None]) -> None:
        """Retire un abonnement de subscribe() ou subscribe_ready()
        (rechargement partiel)."""
        for listeners in (self._listeners, self._ready_listeners):
            if callback in listeners:
                listeners.remove(callback)

    def _emit(self, name: str, value: object = None) -> None:
        """Événement ponctuel (bouton : single, double, hold…) : rien dans l'état, transmis tel
        quel avec sa valeur éventuelle (ex. angle de rotation)."""
        self._fire("action", EventAction(self, now_ms(), name, value))

    def _updated(
        self,
        name: str,
        old: object,
        new: object,
        announce: Callable[[int, object, object], None] | None = None,
    ) -> None:
        """Appelé par State.update : si la valeur change, l'état est diffusé (sauf injoignable :
        il le sera à la connexion), et l'événement déclenché si elle diffère de la dernière
        annoncée. announce(when, old, new) : remplace l'événement Event de l'attribut."""
        if new == old:
            return
        self.changed_at = now = now_ms()
        if self.connected is not False or name == "connected":
            self._broadcast()
        if new is not None and (name not in self._last or self._last[name] != new):
            if announce:
                announce(now, self._last.get(name), new)
            else:
                self._fire(name, Event(self, now, self._last.get(name), new))
            self._last[name] = new

    def _broadcast(self) -> None:
        state = self.state()
        for callback in self._listeners:
            callback(state)

    def _listen[E](self, key: str, func: Listener[E]) -> Listener[E]:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(f"{func.__name__} doit être une fonction async")
        _listeners.append((self, key, func))
        return func

    def _fire(self, key: str, event: object) -> None:
        for device, listened, func in _listeners:
            if device is self and listened == key:
                name = f"{func.__module__.rpartition('.')[2]}.{func.__name__}"
                spawn_task(self._run_listener(func, event, name), name, _listener_tasks)

    async def _run_listener(self, func: Handler, event: object, name: str) -> None:
        async with self._event_lock:  # les tâches démarrent dans l'ordre de création
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
        """Appelé à l'arrêt de l'application : annule les tâches du pilote."""
        await cancel(self._tasks)

    def _spawn(self, coro: Background) -> None:
        """Tâche du pilote (connexion, lecture…) : annulée par stop(), erreur journalisée."""
        spawn_task(coro, self.id, self._tasks)

    def physical(self) -> str:
        """Appareil physique lié (protocole et identité), "" s'il n'y en a pas."""
        return ""

    def details(self) -> dict[str, object]:
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
        }

    def check_measure(self, name: str) -> str:
        """Pourquoi la mesure `name` ne sera jamais reçue ("" si elle peut l'être), d'après son
        état de surveillance."""
        return REASONS.get(self.measures().get(name, ""), "mesure inconnue")

    def state(self) -> dict[str, object]:
        """État envoyé aux pages et au datalog : les attributs connus (None : omis), sans les
        capacités."""
        return {
            state.name: value
            for state in states(type(self))
            if not isinstance(state, Capability)
            and (value := state.__get__(self, type(self))) is not None
        }

    def _background(self, label: str, command: Callable[[], Awaitable[object]]) -> None:
        """Lance command() en tâche de fond, après les commandes précédentes de l'équipement.

        Équipement injoignable ou commande refusée : journalisé, rien ne remonte au script.
        """
        spawn_task(self._background_task(label, command), f"{self.id}.{label}")

    async def _background_task(
        self, label: str, command: Callable[[], Awaitable[object]]
    ) -> None:
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


class IPDevice(Device):
    """Équipement joint par son adresse IP ; BRAND : nom affiché avec l'adresse."""

    BRAND: str = ""

    def __init__(self, host: str, port: int = 0):
        super().__init__()
        self._host = host
        self._port = port

    @override
    def physical(self) -> str:
        return f"{self.BRAND} {self._host}"

    @override
    def details(self) -> dict[str, object]:
        return {"host": self._host}


class Switchable(Device, ABC):
    """Équipement à marche/arrêt. Le pilote met `on` à jour à la réception et fournit
    _switch()."""

    on = State[bool]()
    event_on = EventSource[EventBool]()

    @actionweb
    def switch(self, on: bool, transition: int | None = None) -> None:
        self._background(f"switch({on!r})", lambda: self._switch(on_value(on), transition))

    @actionweb
    def toggle(self, transition: int | None = None) -> None:
        self._background("toggle", lambda: self._switch(not self.on, transition))

    @abstractmethod
    async def _switch(self, on: bool, transition: int | None) -> None: ...
