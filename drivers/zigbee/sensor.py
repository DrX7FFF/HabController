"""Base des appareils Zigbee sur pile (capteurs, boutons) : pas un pilote à déclarer.

Les pilotes climate, motion, contact et button en héritent et ne déclarent que leurs
attributs. Les appareils non standard (Xiaomi…) sont ramenés aux clusters standard par leur
correctif zha-quirks. L'appareil dort : il n'est jamais interrogé."""

import math
from collections.abc import Mapping
from typing import ClassVar, TypedDict

from core.base import Attribute
from drivers.zigbee import ZigbeeDevice, find_cluster, raw_change, setup_reporting, tracked

POWER = 0x0001
ON_OFF = 0x0006
ILLUMINANCE = 0x0400
TEMPERATURE = 0x0402
PRESSURE = 0x0403
HUMIDITY = 0x0405
OCCUPANCY = 0x0406


def lux(value: int) -> int:
    """Luminosité ZCL (10000 × log10(lx) + 1, 0 = trop faible) en lux."""
    return round(10 ** ((value - 1) / 10000)) if value else 0


# (cluster, attribut ZCL) -> (état, conversion de la valeur brute)
MEASURES = {
    (TEMPERATURE, "measured_value"): ("temperature", lambda v: v / 100),
    (HUMIDITY, "measured_value"): ("humidity", lambda v: v / 100),
    (PRESSURE, "measured_value"): ("pressure", lambda v: v),
    (ILLUMINANCE, "measured_value"): ("illuminance", lux),
    (OCCUPANCY, "occupancy"): ("presence", lambda v: bool(v & 1)),
    (ON_OFF, "on_off"): ("open", bool),  # contact de porte : allumé = ouvert
    (POWER, "battery_percentage_remaining"): ("battery", lambda v: round(v / 2)),
}
ZCL = {key: zcl for zcl, (key, _) in MEASURES.items()}  # état -> (cluster, attribut ZCL)
# Envoi spontané : état -> (intervalle min s, max s, écart de reports -> écart en valeur brute)
# (open : envoyé par le capteur de lui-même)
REPORTS = {
    "temperature": (30, 3600, lambda change: change * 100),
    "humidity": (30, 3600, lambda change: change * 100),
    "pressure": (30, 3600, lambda change: change),
    "illuminance": (30, 3600, lambda percent: 10000 * math.log10(1 + percent / 100)),
    "presence": (0, 3600, lambda _: 1),
    "battery": (3600, 43200, lambda change: change * 2),
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
    battery = Attribute[int](readonly=True)

    def __init__(self, ieee: str, reports: SensorReports | None = None):
        super().__init__(ieee)
        self.reports = tracked(self.DEFAULTS, reports)
        self._values: dict[str, bool | float] = {}

    async def _configure(self, zdev) -> None:
        # non suivie (ou d'une autre variante) : le capteur doit cesser de l'envoyer
        for key, (low, high, convert) in REPORTS.items():
            cluster_id, attr = ZCL[key]
            if (cluster := find_cluster(zdev, cluster_id)) is None:
                continue
            cfg = None
            if key in self.reports:
                cfg = (low, high, raw_change(convert(self.reports[key])))
            await setup_reporting(self.id, cluster, {attr: cfg})

    def _cluster(self, zdev, key: str):
        return find_cluster(zdev, ZCL[key][0])

    def _on_attribute(self, cluster_id: int, name: str, value) -> None:
        if (measure := MEASURES.get((cluster_id, name))) and value is not None:
            key, convert = measure
            if key in self.reports:
                self._values[key] = convert(value)

    def _get_connected_state(self) -> dict:
        return dict(self._values)
