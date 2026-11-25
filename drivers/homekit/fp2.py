"""Pilote Aqara FP2 (HomeKit)."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import override

from aiohomekit.model import Accessory
from aiohomekit.model.characteristics import CharacteristicPermissions as Perms
from aiohomekit.model.characteristics import CharacteristicsTypes as Types

from core.base import EventFloat, EventSource, EventZone, PersistentDevice, State
from drivers.homekit import (
    ERRORS,
    PairingError,
    connect,
    links,
    open_session,
    read_pairing_file,
)

log = logging.getLogger(__name__)

FIND_TIMEOUT = 30  # s, attente de l'accessoire sur le réseau (mDNS)
POLL_TIMEOUT = 10  # s, au-delà la connexion est tenue pour morte

SENSOR_INDEX = "C8622A33-826A-4DD3-9BE9-D496361F29BB"  # propre à Aqara

Key = tuple[int, int]  # (aid, iid) : une caractéristique HomeKit


@dataclass
class Entity:
    attr: str
    cast: Callable


@dataclass
class Mapping:
    entities: dict[Key, Entity] = field(default_factory=dict)
    event_keys: set[Key] = field(default_factory=set)


def _zone_name(index: int | None, key: Key, aliases: dict[int, str]) -> str:
    if index is None:  # capteur sans « Sensor Index » : repli sur ses identifiants HomeKit
        return f"zone.{key[0]}_{key[1]}"
    return f"zone.{aliases.get(index, index)}"


def build_mapping(accessories: list[dict], aliases: dict[int, str] | None = None) -> Mapping:
    """Un passage par service, comme buildConfig (node-red-contrib-fp2).

    Chaque service porte au plus un capteur suivi (présence ou luminosité, avec événements) et,
    pour la présence, son « Sensor Index » qui donne le nom de la zone.
    """
    aliases = aliases or {}
    mapping = Mapping()
    tracked = [Types.OCCUPANCY_DETECTED, Types.LIGHT_LEVEL_CURRENT]
    for accessory in map(Accessory.create_from_dict, accessories):
        for service in accessory.services:
            for char in service.characteristics.filter(tracked):
                if Perms.events not in char.perms:
                    continue
                key = (accessory.aid, char.iid)
                if char.type == Types.LIGHT_LEVEL_CURRENT:
                    mapping.entities[key] = Entity("illuminance", float)
                else:
                    zone = _zone_name(service.value(SENSOR_INDEX), key, aliases)
                    mapping.entities[key] = Entity(zone, bool)
    mapping.event_keys = set(mapping.entities)
    return mapping


class FP2(PersistentDevice):
    """Capteur de présence FP2 : une connexion permanente, l'état vient des événements HomeKit."""

    WIDGET = "fp2"
    ERRORS = ERRORS
    FATAL = (PairingError,)
    illuminance = State[float]()  # lx
    event_illuminance = EventSource[EventFloat]()
    event_zone = EventSource[EventZone]()

    @staticmethod
    def config_args() -> list[str]:
        """Arguments écrits par add_device() dans devices/devices.py."""
        return ['zonemapping={0: "Global"}']

    def __init__(
        self,
        find_timeout: float = FIND_TIMEOUT,
        zonemapping: dict[int, str] | None = None,
    ):
        self._zones: dict[str, bool] = {}  # présence par zone : nom -> occupée
        super().__init__()
        self._find_timeout = find_timeout
        self._aliases = {
            int(index): alias
            for index, alias in (zonemapping or {}).items()
            if str(index).isdigit()
        }
        self._subscribed: set[Key] = set()  # abonnements actifs sur la connexion courante
        self._mapping = Mapping()
        self._pairing = None  # défini une fois la première lecture faite

    @property
    def zones(self) -> "Zones":
        return Zones(self)

    # --- cycle de vie -------------------------------------------------------

    @override
    def physical(self) -> str:
        return links().get(self.id, "")

    @override
    def details(self) -> dict:
        try:
            data = read_pairing_file(self.id)
        except (PairingError, ValueError):
            return super().details()
        return {"pairing id": data["hap"]["AccessoryPairingID"]} | data["info"]

    @override
    def check_measure(self, name: str) -> str:
        prefix, _, zone = name.partition(".")
        if prefix != "zone" or not zone:
            return super().check_measure(name)
        # zone sans alias : nommée par son numéro, connu seulement de l'appareil
        if zone.isdigit() or zone in self._aliases.values():
            return ""
        return "zone inconnue (zonemapping)"

    @override
    async def start(self) -> None:
        if self.physical():  # sinon reste injoignable, signalé par devices.yaml
            await super().start()

    @override
    async def _session(self) -> None:
        """Adresse résolue par mDNS à chaque essai."""
        async with open_session() as controller:
            pairing = await connect(controller, self.id, self._find_timeout)
            await self._serve(pairing)

    async def _serve(self, pairing) -> None:
        stops = [
            pairing.dispatcher_connect(self._on_event),
            pairing.dispatcher_availability_changed(self._on_availability),
            pairing.dispatcher_connect_config_changed(self._on_config),
        ]
        self._subscribed = set()  # nouvelle connexion : aucun abonnement
        try:
            await self._sync(pairing)
            self._pairing = pairing
            # La bibliothèque se reconnecte seule et nous prévient, mais ne voit pas une connexion
            # morte sans fermeture TCP : lecture au rythme qu'elle préconise, un échec relance tout.
            while True:
                await asyncio.sleep(pairing.poll_interval.total_seconds())
                async with asyncio.timeout(POLL_TIMEOUT):
                    await self._read(pairing)
        finally:
            for stop in stops:
                stop()
            self._pairing = None

    # --- état ---------------------------------------------------------------

    async def _sync(self, pairing) -> None:
        """Relit les accessoires, refait mapping et abonnements, puis lit les valeurs.

        Mapping et valeurs ne sont remplacés qu'une fois tout lu : en cas d'échec, l'ancien état
        est conservé tel quel.
        """
        mapping = build_mapping(await pairing.list_accessories_and_characteristics(), self._aliases)
        if removed := self._subscribed - mapping.event_keys:
            await pairing.unsubscribe(removed)
            self._subscribed -= removed
        if added := mapping.event_keys - self._subscribed:
            results = await pairing.subscribe(sorted(added)) or {}
            refused = {key for key, value in results.items() if value.get("status", 0) != 0}
            if refused:
                log.warning("%s : abonnement refusé pour %s", self.id, sorted(refused))
            self._subscribed |= added - refused
        values: dict[str, bool | float] = {}
        if mapping.entities:
            values = self._convert(
                mapping, await pairing.get_characteristics(list(mapping.entities))
            )
        self._mapping = mapping
        self._zones = {}
        self._apply_values(values)
        self._set_connected(pairing.is_available, synced=True)

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
                    "%s : valeur illisible pour %s : %r", self.id, entity.attr, result["value"]
                )
        return values

    def _apply(self, updates: dict[Key, dict]) -> None:
        self._apply_values(self._convert(self._mapping, updates))

    def _apply_values(self, values: dict[str, bool | float]) -> None:
        """Valeurs converties : luminosité dans son attribut, présences dans les zones."""
        for attr, value in values.items():
            if attr == "illuminance":
                FP2.illuminance.update(self, value)
            else:
                self._set_zone(attr.removeprefix("zone."), bool(value))

    def _set_zone(self, zone: str, present: bool) -> None:
        old = self._zones.get(zone)
        self._zones[zone] = present
        self._updated(
            f"zone.{zone}",
            old,
            present,
            lambda when, old, new: self._fire(
                "zone", EventZone(self, when, zone, None if old is None else bool(old), bool(new))
            ),
        )

    @override
    def state(self) -> dict:
        """Plus la présence de chaque zone, « zone.<nom> » (noms du datalog)."""
        return super().state() | {f"zone.{zone}": present for zone, present in self._zones.items()}

    @override
    def _reset(self) -> None:
        self._zones = {}
        super()._reset()

    # --- événements de la bibliothèque (appelés dans la boucle asyncio) ------

    def _on_event(self, updates: dict[Key, dict]) -> None:
        if updates:  # un dictionnaire vide signale seulement une reconnexion
            self._apply(updates)

    def _on_availability(self, available: bool) -> None:
        if available == self._connected:
            return
        self._set_connected(available)
        if available and self._pairing is not None:  # sinon synchro initiale en cours
            self._spawn(self._resync(self._pairing))  # l'état a pu changer pendant la coupure

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
                self._set_connected(True, synced=True)
        except ERRORS as err:
            log.warning(
                "%s : lecture de l'état impossible (%s : %s)", self.id, type(err).__name__, err
            )


class Zones:
    """Présence par zone, None tant qu'elle n'est pas connue : `fp2.zones.salon` (alias de
    zonemapping, AttributeError s'il n'y est pas) ou `fp2.zones[1]` (index)."""

    __slots__ = ("_device",)

    def __init__(self, device: FP2):
        self._device = device

    def __getattr__(self, name: str) -> bool | None:
        if name not in self._device._aliases.values():
            raise AttributeError(f"{self._device.id} : zone « {name} » absente de zonemapping")
        return self._device._zones.get(name)

    def __getitem__(self, index: int) -> bool | None:
        return self._device._zones.get(str(self._device._aliases.get(index, index)))
