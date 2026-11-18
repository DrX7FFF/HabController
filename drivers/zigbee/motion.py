"""Pilote détecteur de présence Zigbee."""

from core.base import EventBool, EventInt, EventSource, State
from drivers.zigbee.sensor import ZCL, Sensor, SensorReports, lux


class MotionReports(SensorReports, total=False):
    presence: bool | None
    illuminance: float | None  # % (écart relatif)


class Motion(Sensor):
    DEFAULTS = {"presence": True, "illuminance": 2, "battery": 1}
    presence = State[bool]()
    illuminance = State[int]()  # lx
    event_presence = EventSource[EventBool]()
    event_illuminance = EventSource[EventInt]()
    MEASURES = Sensor.MEASURES | {
        ZCL["presence"]: (presence, lambda v: bool(v & 1)),
        ZCL["illuminance"]: (illuminance, lux),
    }

    def __init__(self, ieee: str, reports: MotionReports | None = None):
        super().__init__(ieee, reports)


Driver = Motion  # convention : le chargeur cherche ce nom
