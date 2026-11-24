"""Pilote détecteur de présence Zigbee."""

from core.base import EventBool, EventInt, EventSource, State
from drivers.zigbee.sensor import Sensor, SensorReports


class MotionReports(SensorReports, total=False):
    presence: bool | None
    illuminance: float | None  # % (écart relatif)


DEFAULTS: MotionReports = {"presence": True, "illuminance": 2, "battery": 1}


class Motion(Sensor):
    DEFAULTS = DEFAULTS
    presence = State[bool]()
    illuminance = State[int]()  # lx
    event_presence = EventSource[EventBool]()
    event_illuminance = EventSource[EventInt]()

    def __init__(self, ieee: str, reports: MotionReports | None = None):
        super().__init__(ieee, reports)


Driver = Motion  # convention : le chargeur cherche ce nom
