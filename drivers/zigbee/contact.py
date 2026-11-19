"""Pilote contact d'ouverture Zigbee."""

from core.base import EventBool, EventSource, State
from drivers.zigbee.sensor import ZCL, Sensor, SensorReports


class ContactReports(SensorReports, total=False):
    open: bool | None


DEFAULTS: ContactReports = {"open": True, "battery": 1}


class Contact(Sensor):
    DEFAULTS = DEFAULTS
    open = State[bool]()
    event_open = EventSource[EventBool]()
    MEASURES = Sensor.MEASURES | {ZCL["open"]: (open, bool)}

    def __init__(self, ieee: str, reports: ContactReports | None = None):
        super().__init__(ieee, reports)


Driver = Contact  # convention : le chargeur cherche ce nom
