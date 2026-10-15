"""Base des appareils Zigbee sur pile (capteurs, boutons) : pas un pilote à déclarer.

Les pilotes climate, motion, contact et button en héritent et ne déclarent que leurs
attributs. Tous ont `battery` (%). Les appareils non standard (Xiaomi…) sont ramenés aux
clusters standard par leur correctif zha-quirks (pile, présence remise à faux après un délai,
luminosité).
L'appareil dort : il n'est jamais interrogé, il est joignable dès son premier message. Après
un redémarrage, une valeur reste absente (None) tant que l'appareil ne l'a pas envoyée.
"""

from core.base import Attribute
from drivers.zigbee import ZigbeeDevice, find_cluster, setup_reporting

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
# Envoi spontané : attribut -> (intervalle min s, max s, écart minimal en valeur brute)
REPORTS = {
    TEMPERATURE: {"measured_value": (30, 3600, 10)},
    HUMIDITY: {"measured_value": (30, 3600, 100)},
    PRESSURE: {"measured_value": (30, 3600, 1)},
    ILLUMINANCE: {"measured_value": (30, 3600, 500)},
    OCCUPANCY: {"occupancy": (0, 3600, 1)},
    POWER: {"battery_percentage_remaining": (3600, 43200, 2)},
}


class Sensor(ZigbeeDevice):
    """Appareil sur pile : l'état ne vient que de ce qu'il envoie."""

    POLL = None
    battery = Attribute[int](readonly=True)

    def __init__(self, name: str = ""):
        super().__init__(name)
        self._values: dict[str, bool | float] = {}

    async def _configure(self, zdev) -> None:
        for cluster_id, reports in REPORTS.items():
            if (cluster := find_cluster(zdev, cluster_id)) is not None:
                await setup_reporting(self.name, cluster, reports)

    def _on_attribute(self, cluster_id: int, name: str, value) -> None:
        if (measure := MEASURES.get((cluster_id, name))) and value is not None:
            key, convert = measure
            self._values[key] = convert(value)

    def _get_connected_state(self) -> dict:
        return dict(self._values)
