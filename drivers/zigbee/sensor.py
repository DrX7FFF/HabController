"""Base des appareils Zigbee sur pile (capteurs, boutons) : pas un pilote à déclarer.

Les pilotes climate, motion, contact et button en héritent et ne déclarent que leurs
attributs. Les appareils non standard (Xiaomi…) sont ramenés aux clusters standard par leur
correctif zha-quirks. L'appareil dort : il n'est jamais interrogé."""

import math
from collections.abc import Mapping
from typing import ClassVar, TypedDict, override

from core.base import EventInt, EventSource, State
from drivers.zigbee import Measure, ZigbeeDevice, find_cluster, tracked

POWER = 0x0001
ON_OFF = 0x0006
ILLUMINANCE = 0x0400
TEMPERATURE = 0x0402
PRESSURE = 0x0403
HUMIDITY = 0x0405
OCCUPANCY = 0x0406


def lux(value: int) -> int:
    """Luminosité ZCL (10000 * log10(lx) + 1, 0 = trop faible) en lux."""
    return round(10 ** ((value - 1) / 10000)) if value else 0


# Mesures de toutes les variantes : celles qu'une variante ne suit pas sont arrêtées sur
# l'appareil (envoi spontané : intervalle min s, max s, écart de reports -> écart brut)
MEASURES = {
    "temperature": Measure(
        TEMPERATURE, ("measured_value",), 30, 3600, lambda change: change * 100,
        lambda v: v / 100,
    ),
    "humidity": Measure(
        HUMIDITY, ("measured_value",), 30, 3600, lambda change: change * 100, lambda v: v / 100
    ),
    "pressure": Measure(PRESSURE, ("measured_value",), 30, 3600, lambda change: change),
    "illuminance": Measure(
        ILLUMINANCE, ("measured_value",), 30, 3600,
        lambda percent: 10000 * math.log10(1 + percent / 100), lux,
    ),
    "presence": Measure(OCCUPANCY, ("occupancy",), 0, 3600, lambda _: 1, lambda v: bool(v & 1)),
    # contact de porte : allumé = ouvert, envoyé par le capteur de lui-même
    "open": Measure(ON_OFF, ("on_off",), value=bool),
    "battery": Measure(
        POWER, ("battery_percentage_remaining",), 3600, 43200, lambda change: change * 2,
        lambda v: round(v / 2),
    ),
}


class SensorReports(TypedDict, total=False):
    """Valeurs suivies : écart minimal entre deux envois (True pour un état tout-ou-rien),
    None : non suivie. Complété par chaque variante."""

    battery: float | None  # %


class Sensor(ZigbeeDevice):
    """Appareil sur pile : l'état ne vient que de ce qu'il envoie."""

    POLL = None
    WIDGET = "sensor"
    # valeurs suivies par défaut, propres à chaque variante
    DEFAULTS: ClassVar[Mapping[str, object]] = {"battery": 1}
    MEASURES = MEASURES
    battery = State[int]()  # %
    event_battery = EventSource[EventInt]()

    def __init__(self, ieee: str, reports: SensorReports | None = None):
        super().__init__(ieee)
        self.reports = tracked(self.DEFAULTS, reports)

    @override
    def _cluster(self, zdev, key: str):
        return find_cluster(zdev, MEASURES[key].cluster, output=True)
