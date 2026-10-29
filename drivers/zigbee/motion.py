"""Pilote détecteur de présence Zigbee."""

from core.base import Attribute
from drivers.zigbee.sensor import Sensor, SensorReports


class MotionReports(SensorReports, total=False):
    presence: bool | None
    illuminance: float | None  # % (écart relatif)


class Motion(Sensor):
    DEFAULTS = {"presence": True, "illuminance": 2, "battery": 1}
    presence = Attribute[bool](readonly=True)
    illuminance = Attribute[int](readonly=True)

    def __init__(self, ieee: str, reports: MotionReports | None = None):
        super().__init__(ieee, reports)


Driver = Motion  # convention : le chargeur cherche ce nom
