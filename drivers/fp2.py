"""Aqara FP2 (HomeKit) : présence globale, zones et luminosité, par événements.

État : {"reachable": bool, "presence": bool, "illuminance": float, "<zone>": bool, ...}.
Les zones portent le nom (en minuscules, sans accents) qu'elles ont dans l'application Aqara.
Le mapping reprend celui de MQTT4FP2. Équipement créé et appairé par pair.py.
"""

import asyncio
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

from aiohomekit.model.characteristics import CharacteristicsTypes as Types
from aiohomekit.uuid import normalize_uuid

from core.base import Device
from core.homekit import ERRORS, PairingError, connect, open_controller

log = logging.getLogger(__name__)

RETRY_MIN = 2  # s, entre deux tentatives de connexion (doublé à chaque échec)
RETRY_MAX = 60
FIND_TIMEOUT = 30  # s, attente de l'accessoire sur le réseau (mDNS)
RESERVED = {"reachable", "presence", "illuminance"}
GLOBAL_NAMES = {
    "global occupancy",
    "global presence",
    "occupancy",
    "occupancy sensor",
    "presence",
    "presence globale",
    "presence global",
    "presence sensor",
}
OCCUPANCY = normalize_uuid(Types.OCCUPANCY_DETECTED)
LIGHT_LEVEL = normalize_uuid(Types.LIGHT_LEVEL_CURRENT)
NAMES = {normalize_uuid(Types.CONFIGURED_NAME), normalize_uuid(Types.NAME)}

Key = tuple[int, int]  # (aid, iid) : une caractéristique HomeKit


@dataclass
class Entity:
    attr: str
    cast: Callable


@dataclass
class Mapping:
    entities: dict[Key, Entity] = field(default_factory=dict)
    event_keys: set[Key] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def _slug(text: str) -> str:
    ascii_text = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    )
    return re.sub(r"[^a-z0-9]+", "_", ascii_text).strip("_")


def _is_global(name: str) -> bool:
    return _slug(name).replace("_", " ") in GLOBAL_NAMES


def _service_name(service: dict) -> str:
    for characteristic in service.get("characteristics", []):
        if normalize_uuid(str(characteristic.get("type", ""))) in NAMES:
            value = characteristic.get("value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    value = service.get("name")
    return value.strip() if isinstance(value, str) else ""


def build_mapping(accessories: list[dict]) -> Mapping:
    """Repère présence globale, zones et luminosité parmi les caractéristiques à événements."""
    mapping = Mapping()
    occupancy: list[tuple[Key, str]] = []
    light: list[tuple[Key, str]] = []
    for accessory in accessories:
        aid = int(accessory.get("aid", 0))
        for service in accessory.get("services", []):
            name = _service_name(service)
            for characteristic in service.get("characteristics", []):
                if "ev" not in characteristic.get("perms", []):
                    continue
                key = (aid, int(characteristic.get("iid", 0)))
                kind = normalize_uuid(str(characteristic.get("type", "")))
                if kind == OCCUPANCY:
                    occupancy.append((key, name))
                elif kind == LIGHT_LEVEL:
                    light.append((key, name))

    named_global = [item for item in occupancy if _is_global(item[1])]
    global_key: Key | None = None
    if len(occupancy) == 1:
        global_key = occupancy[0][0]
    elif len(named_global) == 1:
        global_key = named_global[0][0]
    elif occupancy:
        mapping.warnings.append(
            "présence globale introuvable ou ambiguë (aucun nom ne la distingue)"
        )
    if global_key:
        mapping.entities[global_key] = Entity("presence", bool)

    used = set(RESERVED)
    for key, name in occupancy:
        if key == global_key:
            continue
        attr = _slug(name)
        if not attr or _is_global(name):
            mapping.warnings.append(f"zone {key} ignorée (nom « {name} » inexploitable)")
            continue
        if attr in used:
            attr = f"{attr}_{key[0]}_{key[1]}"
        used.add(attr)
        mapping.entities[key] = Entity(attr, bool)

    candidates = light if len(light) <= 1 else [item for item in light if _is_global(item[1])]
    if len(candidates) == 1:
        mapping.entities[candidates[0][0]] = Entity("illuminance", float)
    elif light:
        mapping.warnings.append(
            "plusieurs capteurs de luminosité, aucun n'est désigné comme global"
        )

    mapping.event_keys = set(mapping.entities)
    return mapping


class FP2(Device):
    """Capteur de présence FP2 : une connexion permanente, l'état vient des événements HomeKit."""

    capabilities = frozenset({"presence", "illuminance"})

    def __init__(
        self,
        id: str,
        name: str,
        capabilities: list[str] | None = None,
        find_timeout: float = FIND_TIMEOUT,
    ):
        super().__init__(id, name, capabilities)
        self._find_timeout = find_timeout
        self._mapping = Mapping()
        self._values: dict[str, bool | float] = {}  # dernières valeurs connues, par attribut
        self._pairing = None  # défini une fois la première lecture faite
        self._connected = False
        self._synced = False  # valeurs lues au moins une fois depuis la connexion courante
        self._backoff = RETRY_MIN
        self._tasks: set[asyncio.Task] = set()

    # --- cycle de vie -------------------------------------------------------

    async def start(self) -> None:
        self._spawn(self._run())

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (err := task.exception()):
            log.error("%s : erreur inattendue", self.name, exc_info=err)

    async def _run(self) -> None:
        """Se connecte (adresse résolue par mDNS à chaque essai) et réessaie tant qu'il le faut."""
        while True:
            try:
                async with open_controller() as controller:
                    pairing = await connect(controller, self.id, self._find_timeout)
                    await self._serve(pairing)
            except PairingError as err:
                log.error("%s : %s", self.name, err)
                return  # inutile de réessayer
            except ERRORS as err:
                log.warning(
                    "%s : connexion impossible (%s : %s), nouvel essai dans %s s",
                    self.name,
                    type(err).__name__,
                    err,
                    self._backoff,
                )
            self._reset()
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, RETRY_MAX)

    async def _serve(self, pairing) -> None:
        stops = [
            pairing.dispatcher_connect(self._on_event),
            pairing.dispatcher_availability_changed(self._on_availability),
            pairing.dispatcher_connect_config_changed(self._on_config),
        ]
        try:
            await self._sync(pairing)
            self._backoff = RETRY_MIN
            self._pairing = pairing
            # La bibliothèque se reconnecte seule et nous prévient : on attend l'arrêt.
            await asyncio.Event().wait()
        finally:
            for stop in stops:
                stop()
            self._pairing = None

    def _reset(self) -> None:
        self._connected = False
        self._synced = False
        self._notify()

    # --- état ---------------------------------------------------------------

    async def _sync(self, pairing) -> None:
        """Relit les accessoires, refait mapping et abonnements, puis lit les valeurs."""
        mapping = build_mapping(await pairing.list_accessories_and_characteristics())
        for warning in mapping.warnings:
            log.warning("%s : mapping incomplet, %s", self.name, warning)
        old_keys = self._mapping.event_keys
        self._mapping = mapping  # avant l'abonnement : un événement précoce doit être reconnu
        if removed := old_keys - mapping.event_keys:
            await pairing.unsubscribe(removed)
        if added := mapping.event_keys - old_keys:
            failures = {
                key: value
                for key, value in (await pairing.subscribe(sorted(added)) or {}).items()
                if value.get("status", 0) != 0
            }
            if failures:
                log.warning("%s : abonnement refusé pour %s", self.name, sorted(failures))
        self._values = {}
        await self._read(pairing)
        self._connected = pairing.is_available
        self._synced = True
        self._notify()

    async def _read(self, pairing) -> None:
        if self._mapping.entities:
            self._apply(await pairing.get_characteristics(list(self._mapping.entities)))

    def _apply(self, updates: dict[Key, dict]) -> None:
        for key, result in updates.items():
            entity = self._mapping.entities.get(key)
            if entity is None or "value" not in result:
                continue
            try:
                self._values[entity.attr] = entity.cast(result["value"])
            except (TypeError, ValueError):
                log.warning(
                    "%s : valeur illisible pour %s : %r", self.name, entity.attr, result["value"]
                )

    def state(self) -> dict:
        if not (self._connected and self._synced):
            return {"reachable": False}
        return {"reachable": True, **self._values}

    # --- événements de la bibliothèque (appelés dans la boucle asyncio) ------

    def _on_event(self, updates: dict[Key, dict]) -> None:
        if updates:  # un dictionnaire vide signale seulement une reconnexion
            self._apply(updates)
            self._notify()

    def _on_availability(self, available: bool) -> None:
        if available == self._connected:
            return
        self._connected = available
        self._synced = False
        if available and self._pairing is not None:  # l'état a pu changer pendant la coupure
            self._spawn(self._resync(self._pairing))
        self._notify()

    def _on_config(self, _config_num: int) -> None:
        """L'accessoire a changé de configuration (zones modifiées dans l'appli Aqara)."""
        if self._pairing is not None:
            self._spawn(self._resync(self._pairing, full=True))

    async def _resync(self, pairing, full: bool = False) -> None:
        try:
            if full:
                await self._sync(pairing)
            else:
                await self._read(pairing)
                self._synced = True
                self._notify()
        except ERRORS as err:
            log.warning(
                "%s : lecture de l'état impossible (%s : %s)", self.name, type(err).__name__, err
            )

    # --- commandes ----------------------------------------------------------

    async def set(self, name: str, value: str | int, transition: int | None = None) -> None:
        raise ValueError(f"{self.name} est un capteur : aucune commande")


Driver = FP2  # convention : le chargeur cherche ce nom
