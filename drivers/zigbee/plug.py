"""Pilote prise Zigbee."""

import logging
from typing import TypedDict, override

from core.base import EventFloat, EventSource, State, Switchable
from drivers.zigbee import Measure, ZigbeeDevice, find_cluster, read, tracked

log = logging.getLogger(__name__)

ON_OFF = 0x0006
METERING = 0x0702
ELECTRICAL = 0x0B04

# état -> mesure (envoi spontané : intervalle min s, max s) ; écart et valeur des mesures
# électriques : mis à l'échelle par le pilote (FACTORS)
MEASURES = {
    "on": Measure(ON_OFF, ("on_off",), 0, 600, lambda _: 1, bool),
    "active_power": Measure(ELECTRICAL, ("active_power",), 5, 300, lambda change: change),
    "voltage": Measure(ELECTRICAL, ("rms_voltage",), 30, 600, lambda change: change),
    "current": Measure(ELECTRICAL, ("rms_current",), 5, 300, lambda change: change),
    "energy": Measure(METERING, ("current_summ_delivered",), 30, 600, lambda change: change),
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


class Plug(Switchable, ZigbeeDevice):
    """Prise Zigbee : marche/arrêt et mesures, envoyées spontanément par la prise."""

    DEFAULTS = DEFAULTS
    MEASURES = MEASURES
    WIDGET = "plug"

    active_power = State[float]()  # W
    voltage = State[float]()  # V
    current = State[float]()  # A
    energy = State[float]()  # kWh
    event_active_power = EventSource[EventFloat]()
    event_voltage = EventSource[EventFloat]()
    event_current = EventSource[EventFloat]()
    event_energy = EventSource[EventFloat]()

    def __init__(
        self, ieee: str, poll: float | None = None, reports: PlugReports | None = None
    ):
        super().__init__(ieee, poll)
        self.reports = tracked(DEFAULTS, reports)
        self._scales: dict[str, float] = {}  # mesure suivie -> facteur, si la prise l'a

    # --- état ---------------------------------------------------------------

    @override
    async def _configure(self, zdev) -> None:
        # facteur absent de l'appareil : fourni par son correctif zha-quirks, sinon 1
        scales = {}
        for key, (mult_attr, div_attr) in FACTORS.items():
            cluster = self._cluster(zdev, key)
            if cluster is None or key not in self.reports:
                continue
            factors = await read(cluster, [mult_attr, div_attr])
            multiplier = factors.get(mult_attr) or 1
            divisor = factors.get(div_attr) or 1
            scales[key] = multiplier / divisor
        self._scales = scales
        await super()._configure(zdev)

    @override
    def _change(self, key: str) -> float:
        return super()._change(key) / self._scales[key] if key in self._scales else 1

    @override
    def _value(self, key: str, value):
        if key not in FACTORS:
            return super()._value(key, value)
        return round(value * self._scales[key], 3) if key in self._scales else None

    # --- commandes ----------------------------------------------------------

    @override
    async def _switch(self, on: bool, transition: int | None) -> None:
        cluster = find_cluster(self.zdev, ON_OFF) if self.zdev is not None else None
        await self._command(cluster, "on" if on else "off")  # commandes ZCL « on » / « off »
        Plug.on.update(self, on)


Driver = Plug  # convention : le chargeur cherche ce nom
