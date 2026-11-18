"""Pilote contact d'ouverture Zigbee."""

from core.base import EventBool, EventSource, State
from drivers.zigbee.sensor import ZCL, Sensor, SensorReports


class ContactReports(SensorReports, total=False):
    open: bool | None


class Contact(Sensor):
    DEFAULTS = {"open": True, "battery": 1}
    open = State[bool]()
    event_open = EventSource[EventBool]()
    MEASURES = Sensor.MEASURES | {ZCL["open"]: (open, bool)}

    def __init__(self, ieee: str, reports: ContactReports | None = None):
        super().__init__(ieee, reports)


Driver = Contact  # convention : le chargeur cherche ce nom
