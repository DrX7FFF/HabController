"""Pilote prise Zigbee."""

import logging
from typing import TypedDict

from core.base import Attribute, on_value
from drivers.zigbee import (
    ZigbeeDevice,
    find_cluster,
    raw_change,
    read,
    setup_reporting,
    tracked,
)

log = logging.getLogger(__name__)

ON_OFF = 0x0006
METERING = 0x0702
ELECTRICAL = 0x0B04

# état : (cluster, attribut ZCL, envoi spontané : intervalle min s, max s)
ZCL = {
    "on": (ON_OFF, "on_off", 0, 600),
    "active_power": (ELECTRICAL, "active_power", 5, 300),
    "voltage": (ELECTRICAL, "rms_voltage", 30, 600),
    "current": (ELECTRICAL, "rms_current", 5, 300),
    "energy": (METERING, "current_summ_delivered", 30, 600),
}
# mesure : (multiplicateur, diviseur), attributs de son cluster ; valeur = brute * mult / div
FACTORS = {
    "active_power": ("ac_power_multiplier", "ac_power_divisor"),
    "voltage": ("ac_voltage_multiplier", "ac_voltage_divisor"),
    "current": ("ac_current_multiplier", "ac_current_divisor"),
    "energy": ("multiplier", "divisor"),
}


class PlugReports(TypedDict, total=False):
    """Valeurs suivies : écart minimal entre deux envois (True pour on), None : non suivie."""

    on: bool | None
    active_power: float | None  # W
    voltage: float | None  # V
    current: float | None  # A
    energy: float | None  # kWh


DEFAULTS: PlugReports = {
    "on": True,
    "active_power": 1,
    "voltage": None,
    "current": None,
    "energy": None,
}


class Plug(ZigbeeDevice):
    """Prise Zigbee : marche/arrêt et mesures, envoyées spontanément par la prise."""

    on = Attribute[bool]()  # True / False ; "toggle" accepté en écriture
    active_power = Attribute[float](readonly=True)
    voltage = Attribute[float](readonly=True)
    current = Attribute[float](readonly=True)
    energy = Attribute[float](readonly=True)

    def __init__(
        self, ieee: str, poll: float | None = None, reports: PlugReports | None = None
    ):
        super().__init__(ieee, poll)
        self.reports = tracked(DEFAULTS, reports)
        self._values: dict[str, bool | float] = {}
        self._scales: dict[str, float] = {}  # mesure suivie -> facteur, si la prise l'a
        self._by_zcl = {(cluster_id, attr): key for key, (cluster_id, attr, _, _) in ZCL.items()}

    # --- état ---------------------------------------------------------------

    async def _configure(self, zdev) -> None:
        # facteur absent de l'appareil : fourni par son correctif zha-quirks, sinon 1
        scales = {}
        for key, (mult_attr, div_attr) in FACTORS.items():
            cluster = find_cluster(zdev, ZCL[key][0])
            if cluster is None or key not in self.reports:
                continue
            factors = await read(cluster, [mult_attr, div_attr])
            multiplier = factors.get(mult_attr) or 1
            divisor = factors.get(div_attr) or 1
            scales[key] = multiplier / divisor
        self._scales = scales
        # non suivie : la prise doit cesser de l'envoyer (elle garde sa configuration)
        reports: dict[int, dict] = {}
        for key, (cluster_id, attr, low, high) in ZCL.items():
            cfg = None
            if key in self.reports:
                change = self.reports[key] / scales[key] if key in scales else 1
                cfg = (low, high, raw_change(change))
            reports.setdefault(cluster_id, {})[attr] = cfg
        for cluster_id, cluster_reports in reports.items():
            if (cluster := find_cluster(zdev, cluster_id)) is not None:
                await setup_reporting(self.id, cluster, cluster_reports)

    async def _read(self, zdev) -> None:
        attrs: dict[int, list[str]] = {}
        for key in self.reports:
            cluster_id, attr, _, _ = ZCL[key]
            attrs.setdefault(cluster_id, []).append(attr)
        for cluster_id, names in attrs.items():
            if (cluster := find_cluster(zdev, cluster_id)) is None:
                continue
            for name, value in (await read(cluster, names)).items():
                self._on_attribute(cluster_id, name, value)

    def _on_attribute(self, cluster_id: int, name: str, value) -> None:
        key = self._by_zcl.get((cluster_id, name))
        if key not in self.reports:
            return
        if key == "on":
            self._values["on"] = bool(value)
        elif key in self._scales:
            self._values[key] = round(value * self._scales[key], 3)

    def _get_connected_state(self) -> dict:
        return dict(self._values)

    # --- commandes ----------------------------------------------------------

    async def set(self, name: str, value: bool | str | int, transition: int | None = None) -> None:
        if name != "on":
            raise ValueError(f"{self.id} ne gère pas « {name} »")
        on = on_value(value, self._values.get("on") is True)
        cluster = find_cluster(self.zdev, ON_OFF) if self.zdev is not None else None
        await self._command(cluster, "on" if on else "off")  # commandes ZCL « on » / « off »
        self._values["on"] = on
        self._notify()


Driver = Plug  # convention : le chargeur cherche ce nom
