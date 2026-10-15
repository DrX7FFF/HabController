"""Prise ou relais Zigbee : marche/arrêt, et mesures électriques si l'équipement en fait.

État : {"connected": bool, "on": bool,
        "active_power": W, "voltage": V, "current": A, "energy": kWh}
Les mesures n'apparaissent que si l'équipement a les clusters correspondants (0x0b04, 0x0702).
Chaque prise se déclare dans un fichier de `devices/`, sans adresse : le lien avec l'appareil
physique est une ligne de pairing/zigbee.toml. Les deux sont créés par `./habctrl pair zigbee` :

    from drivers.zigbee.plug import Plug

    prise_salon = Plug(name="Prise salon")

Écriture : prise_salon.on = True | False | "toggle", comme une ampoule.
"""

import logging

from core.base import Attribute, on_value
from drivers.zigbee import ZigbeeDevice, find_cluster, read, setup_reporting

log = logging.getLogger(__name__)

ON_OFF = 0x0006
METERING = 0x0702
ELECTRICAL = 0x0B04

# état : (cluster, attribut ZCL, multiplicateur, diviseur) ; valeur = brute * mult / div
MEASURES = {
    "active_power": (ELECTRICAL, "active_power", "ac_power_multiplier", "ac_power_divisor"),
    "voltage": (ELECTRICAL, "rms_voltage", "ac_voltage_multiplier", "ac_voltage_divisor"),
    "current": (ELECTRICAL, "rms_current", "ac_current_multiplier", "ac_current_divisor"),
    "energy": (METERING, "current_summ_delivered", "multiplier", "divisor"),
}
# Diviseurs des modèles qui ne les annoncent pas (sinon 1), comme les quirks de ZHA.
MODEL_DIVISORS = {
    "TS011F": {"energy": 100, "current": 1000},  # Tuya : centièmes de kWh, mA
}
# Envoi spontané : attribut -> (intervalle min s, max s, écart minimal en valeur brute)
REPORTS = {
    ON_OFF: {"on_off": (0, 600, 1)},
    ELECTRICAL: {
        "active_power": (5, 300, 1),
        "rms_voltage": (30, 600, 1),
        "rms_current": (5, 300, 1),
    },
    METERING: {"current_summ_delivered": (30, 600, 1)},
}


class Plug(ZigbeeDevice):
    """Prise Zigbee : marche/arrêt et mesures, envoyées spontanément par la prise."""

    on = Attribute[bool]()  # True / False ; "toggle" accepté en écriture
    active_power = Attribute[float](readonly=True)
    voltage = Attribute[float](readonly=True)
    current = Attribute[float](readonly=True)
    energy = Attribute[float](readonly=True)

    def __init__(self, name: str = "", poll: float | None = None):
        super().__init__(name, poll)
        self._values: dict[str, bool | float] = {}
        self._scales: dict[str, float] = {}  # état -> facteur, pour les mesures disponibles
        self._by_zcl: dict[tuple[int, str], str] = {
            (cluster_id, attr): key for key, (cluster_id, attr, _, _) in MEASURES.items()
        }

    # --- état ---------------------------------------------------------------

    async def _configure(self, zdev) -> None:
        defaults = MODEL_DIVISORS.get(zdev.model or "", {})
        scales = {}
        for key, (cluster_id, _attr, mult_attr, div_attr) in MEASURES.items():
            cluster = find_cluster(zdev, cluster_id)
            if cluster is None:
                continue
            factors = await read(cluster, [mult_attr, div_attr])
            multiplier = factors.get(mult_attr) or 1
            divisor = factors.get(div_attr) or defaults.get(key, 1)
            scales[key] = multiplier / divisor
        self._scales = scales
        for cluster_id, reports in REPORTS.items():
            if (cluster := find_cluster(zdev, cluster_id)) is not None:
                await setup_reporting(self.name, cluster, reports)

    async def _read(self, zdev) -> None:
        for cluster_id, reports in REPORTS.items():
            if (cluster := find_cluster(zdev, cluster_id)) is None:
                continue
            for name, value in (await read(cluster, list(reports))).items():
                self._on_attribute(cluster_id, name, value)

    def _on_attribute(self, cluster_id: int, name: str, value) -> None:
        if cluster_id == ON_OFF and name == "on_off":
            self._values["on"] = bool(value)
        elif (key := self._by_zcl.get((cluster_id, name))) and key in self._scales:
            self._values[key] = round(value * self._scales[key], 3)

    def _get_connected_state(self) -> dict:
        return dict(self._values)

    # --- commandes ----------------------------------------------------------

    async def set(self, name: str, value: bool | str | int, transition: int | None = None) -> None:
        if name != "on":
            raise ValueError(f"{self.name} ne gère pas « {name} »")
        on = on_value(value, self._values.get("on") is True)
        cluster = find_cluster(self.zdev, ON_OFF) if self.zdev is not None else None
        await self._command(cluster, "on" if on else "off")  # commandes ZCL « on » / « off »
        self._values["on"] = on
        self._notify()


Driver = Plug  # convention : le chargeur cherche ce nom
