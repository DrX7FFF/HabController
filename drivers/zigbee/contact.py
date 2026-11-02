"""Pilote contact d'ouverture Zigbee."""

from core.base import Attribute, EventBool, EventSource
from drivers.zigbee.sensor import Sensor, SensorReports


class ContactReports(SensorReports, total=False):
    open: bool | None


class Contact(Sensor):
    DEFAULTS = {"open": True, "battery": 1}
    open = Attribute[bool](readonly=True)
    event_open = EventSource[EventBool]()

    def __init__(self, ieee: str, reports: ContactReports | None = None):
        super().__init__(ieee, reports)


Driver = Contact  # convention : le chargeur cherche ce nom
