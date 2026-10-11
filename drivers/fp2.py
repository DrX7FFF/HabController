"""Aqara FP2 (HomeKit) : capteurs de présence et luminosité, par événements.

État : {"reachable": bool, "zoneGlobal": bool, "zone1": bool, ..., "illuminance": float}.
Chaque capteur de présence est repéré par son « Sensor Index » (caractéristique Aqara), comme
dans node-red-contrib-fp2 : l'index 0 est la présence globale (zoneGlobal, non renommable), les
autres s'appellent zone1, zone2... Dans devices.toml, on les renomme à volonté, sans toucher
au reste :

    [fp2]
    name = "FP2"
    driver = "fp2"
    zone1 = "salon"      # index 1 -> attribut « salon » ; une zone non listée garde « zoneN »

Équipement créé et appairé par pair.py.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aiohomekit.model.characteristics import CharacteristicsTypes as Types
from aiohomekit.uuid import normalize_uuid

from core.base import Device
from core.homekit import ERRORS, PairingError, connect, open_controller

log = logging.getLogger(__name__)

RETRY_MIN = 2  # s, entre deux tentatives de connexion (doublé à chaque échec)
RETRY_MAX = 60
FIND_TIMEOUT = 30  # s, attente de l'accessoire sur le réseau (mDNS)
OCCUPANCY = normalize_uuid(Types.OCCUPANCY_DETECTED)
LIGHT_LEVEL = normalize_uuid(Types.LIGHT_LEVEL_CURRENT)
SENSOR_INDEX = normalize_uuid("C8622A33-826A-4DD3-9BE9-D496361F29BB")  # propre à Aqara
GLOBAL = "zoneGlobal"  # index 0 : présence globale
RESERVED = {"reachable", "illuminance", GLOBAL}
ZONE_KEY = re.compile(r"zone([1-9]\d*)")  # clés de devices.toml : zone1, zone2...
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

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


def parse_aliases(zones: dict[str, Any]) -> dict[int, str]:
    """Paramètres « zoneN = nom » de devices.toml -> {index: nom} ; lève ValueError si invalides."""
    aliases: dict[int, str] = {}
    for key, alias in zones.items():
        if key == GLOBAL:
            raise ValueError(f"{GLOBAL} (index 0) n'est pas renommable")
        match = ZONE_KEY.fullmatch(key)
        if not match:
            raise ValueError(f"paramètre inconnu « {key} » (attendu zone1, zone2...)")
        if not isinstance(alias, str) or not NAME.fullmatch(alias):
            raise ValueError(f"{key} : nom invalide « {alias} » (lettres, chiffres et _)")
        taken = alias in RESERVED or alias in aliases.values()
        if taken or (ZONE_KEY.fullmatch(alias) and alias != key):
            raise ValueError(f"{key} : nom « {alias} » réservé ou déjà utilisé")
        aliases[int(match[1])] = alias
    return aliases


def _zone_name(index: int | None, key: Key, aliases: dict[int, str]) -> str:
    if index is None:  # capteur sans « Sensor Index » : repli sur ses identifiants HomeKit
        return f"zone_{key[0]}_{key[1]}"
    if index == 0:
        return GLOBAL
    return aliases.get(index, f"zone{index}")


def build_mapping(accessories: list[dict], aliases: dict[int, str] | None = None) -> Mapping:
    """Un attribut par capteur de présence (zoneGlobal, zone1...) et un pour la luminosité."""
    aliases = aliases or {}
    mapping = Mapping()
    light: list[Key] = []
    for accessory in accessories:
        aid = int(accessory.get("aid", 0))
        for service in accessory.get("services", []):
            index = None
            sensors: list[Key] = []
            for characteristic in service.get("characteristics", []):
                kind = normalize_uuid(str(characteristic.get("type", "")))
                key = (aid, int(characteristic.get("iid", 0)))
                if kind == SENSOR_INDEX:
                    try:
                        index = int(characteristic.get("value"))
                    except (TypeError, ValueError):
                        index = None
                elif "ev" in characteristic.get("perms", []):
                    if kind == OCCUPANCY:
                        sensors.append(key)
                    elif kind == LIGHT_LEVEL:
                        light.append(key)
            for key in sensors:
                attr = _zone_name(index, key, aliases)
                if attr in {entity.attr for entity in mapping.entities.values()}:
                    mapping.warnings.append(f"capteur {key} ignoré : « {attr} » déjà utilisé")
                    continue
                mapping.entities[key] = Entity(attr, bool)

    for key in light:
        attr = "illuminance" if len(light) == 1 else f"illuminance_{key[0]}_{key[1]}"
        mapping.entities[key] = Entity(attr, float)

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
        **zones: str,  # zone1 = "salon", zone2 = "cuisine"... (devices.toml)
    ):
        super().__init__(id, name, capabilities)
        self._find_timeout = find_timeout
        self._aliases = parse_aliases(zones)
        self._subscribed: set[Key] = set()  # abonnements actifs sur la connexion courante
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
        self._subscribed = set()  # nouvelle connexion : aucun abonnement
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
        """Relit les accessoires, refait mapping et abonnements, puis lit les valeurs.

        Mapping et valeurs ne sont remplacés qu'une fois tout lu : en cas d'échec, l'ancien état
        est conservé tel quel.
        """
        mapping = build_mapping(await pairing.list_accessories_and_characteristics(), self._aliases)
        for warning in mapping.warnings:
            log.warning("%s : mapping incomplet, %s", self.name, warning)
        if removed := self._subscribed - mapping.event_keys:
            await pairing.unsubscribe(removed)
            self._subscribed -= removed
        if added := mapping.event_keys - self._subscribed:
            results = await pairing.subscribe(sorted(added)) or {}
            refused = {key for key, value in results.items() if value.get("status", 0) != 0}
            if refused:
                log.warning("%s : abonnement refusé pour %s", self.name, sorted(refused))
            self._subscribed |= added - refused
        values: dict[str, bool | float] = {}
        if mapping.entities:
            values = self._convert(
                mapping, await pairing.get_characteristics(list(mapping.entities))
            )
        self._mapping = mapping
        self._values = values
        self._connected = pairing.is_available
        self._synced = True
        self._notify()

    async def _read(self, pairing) -> None:
        if self._mapping.entities:
            self._apply(await pairing.get_characteristics(list(self._mapping.entities)))

    def _convert(self, mapping: Mapping, updates: dict[Key, dict]) -> dict[str, bool | float]:
        values: dict[str, bool | float] = {}
        for key, result in updates.items():
            entity = mapping.entities.get(key)
            if entity is None or "value" not in result:
                continue
            try:
                values[entity.attr] = entity.cast(result["value"])
            except (TypeError, ValueError):
                log.warning(
                    "%s : valeur illisible pour %s : %r", self.name, entity.attr, result["value"]
                )
        return values

    def _apply(self, updates: dict[Key, dict]) -> None:
        self._values.update(self._convert(self._mapping, updates))

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
