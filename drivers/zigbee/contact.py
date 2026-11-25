"""Pilote contact d'ouverture Zigbee."""

from core.base import EventBool, EventSource, State
from drivers.zigbee.sensor import Sensor, SensorReports


class ContactReports(SensorReports, total=False):
    open: bool | None


DEFAULTS: ContactReports = {"open": True, "battery": 1}


class Contact(Sensor):
    DEFAULTS = DEFAULTS
    open = State[bool]()
    event_open = EventSource[EventBool]()

    def __init__(self, ieee: str, reports: ContactReports | None = None):
        super().__init__(ieee, reports)
