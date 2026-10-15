import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar, overload

log = logging.getLogger(__name__)
T = TypeVar("T")

tasks: set[asyncio.Task] = set()  # tâches de fond (commandes, automatisations), annulées à l'arrêt


def _done(task: asyncio.Task) -> None:
    tasks.discard(task)
    if not task.cancelled() and (err := task.exception()):
        log.error("%s : erreur inattendue", task.get_name(), exc_info=err)


def spawn_task(coro, name: str) -> None:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    tasks.add(task)
    task.add_done_callback(_done)


class DeviceError(Exception):
    """Équipement injoignable ou commande refusée."""


def on_value(value: bool | str | int, current: bool | None) -> bool:
    """Valeur demandée pour l'attribut `on` : True, False, ou "toggle" (inverse l'état connu)."""
    if value == "toggle":
        return not current
    if isinstance(value, bool):
        return value
    raise ValueError("on attend True, False ou « toggle »")


class Attribute(Generic[T]):
    """Attribut d'un équipement : lecture dans l'état mémorisé (None si absent, ex. injoignable),
    écriture = commande envoyée en tâche de fond. Le nom est celui de la clé d'état."""

    def __init__(self, readonly: bool = False):
        self.readonly = readonly
        self.name = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    @overload
    def __get__(self, obj: None, owner: type) -> "Attribute[T]": ...
    @overload
    def __get__(self, obj: "Device", owner: type) -> T | None: ...
    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        return obj.state().get(self.name)

    def __set__(self, obj: "Device", value: T) -> None:
        if self.readonly:
            raise AttributeError(f"{self.name} est en lecture seule")
        obj._assign(self.name, value)  # pyright: ignore[reportArgumentType]


class Device:
    """Base commune des pilotes : identité, état et commandes."""

    connected = Attribute[bool](readonly=True)

    def __init__(self, name: str = ""):
        self.id = ""  # affecté par configure() : le chargeur le déduit du nom de la variable
        self.name = name
        self._listeners: list[Callable[[dict], None]] = []
        self._lock = asyncio.Lock()  # les commandes d'un même équipement partent dans l'ordre
        self._last_notified: dict | None = None
        self.changed_at: int | None = None  # ms depuis l'époque Unix, dernier changement d'état

    def configure(self, dev_id: str) -> None:
        """Appelé par le chargeur, avant start() : identifiant, et nom par défaut."""
        self.id = dev_id
        if not self.name:
            self.name = dev_id

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        """callback(state) est appelé à chaque changement d'état de l'équipement."""
        self._listeners.append(callback)

    def _notify(self) -> None:
        """À appeler après toute mise à jour de l'état ; ne diffuse que les vrais changements.

        Le changement est horodaté (changed_at) avant d'être diffusé.
        """
        state = self.state()
        if state != self._last_notified:
            self._last_notified = state
            self.changed_at = time.time_ns() // 1_000_000
            for callback in self._listeners:
                callback(state)

    async def start(self) -> None:
        """Appelé au démarrage de l'application (connexion, écoute...)."""

    async def stop(self) -> None:
        """Appelé à l'arrêt de l'application."""

    def physical(self) -> str:
        """Appareil physique lié (protocole et identité), "" s'il n'y en a pas."""
        return ""

    def state(self) -> dict:
        """État courant, lu en mémoire : ne doit jamais interroger l'équipement."""
        raise NotImplementedError

    async def set(self, name: str, value: bool | str | int, transition: int | None = None) -> None:
        raise NotImplementedError

    async def send(self, name: str, value: bool | str | int, transition: int | None = None) -> bool:
        """set() après les autres commandes : False (journalisé) si l'équipement n'a pas répondu.

        Une commande refusée par le pilote (ValueError) est une erreur du script : elle remonte.
        """
        async with self._lock:
            try:
                await self.set(name, value, transition)
            except DeviceError as err:
                log.warning("%s : commande %s=%s non envoyée (%s)", self.id, name, value, err)
                return False
        return True

    def _assign(self, name: str, value: bool | str | int) -> None:
        """Écriture `equipement.attribut = valeur` : commande en tâche de fond, sans bloquer."""
        self._background(f"{name}={value!r}", lambda: self.set(name, value))

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

    def __init__(self, name: str = ""):
        super().__init__(name)
        self._connected = False
        self._synced = False  # état lu au moins une fois depuis la connexion courante

    def state(self) -> dict:
        """Retourne l'état : {"connected": False} si injoignable, sinon {"connected": True, ...}."""
        if not (self._connected and self._synced):
            return {"connected": False, **self._get_disconnected_state()}
        return {"connected": True, **self._get_connected_state()}

    def _get_disconnected_state(self) -> dict:
        """Valeurs par défaut quand injoignable. Peut être surchargé par les subclasses."""
        return {}

    def _get_connected_state(self) -> dict:
        """L'état quand connecté et synchronisé. À implémenter par les subclasses."""
        return {}
