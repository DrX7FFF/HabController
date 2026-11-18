"""Pilote contact d'ouverture Zigbee."""

from core.base import EventBool, EventSource, State, known
from drivers.zigbee.sensor import Sensor, SensorReports


class ContactReports(SensorReports, total=False):
    open: bool | None


class Contact(Sensor):
    DEFAULTS = {"open": True, "battery": 1}
    open = State[bool]()
    event_open = EventSource[EventBool]()

    def __init__(self, ieee: str, reports: ContactReports | None = None):
        super().__init__(ieee, reports)

    def state(self) -> dict:
        state = super().state()
        if not state.get("connected"):
            return state
        return state | known(open=self.open)

    def measures(self) -> dict[str, str]:
        return dict.fromkeys(("open",), "tracked") | super().measures()

    def _reset(self) -> None:
        Contact.open.update(self, None)
        super()._reset()

    def _measure(self, key: str, value) -> None:
        match key:
            case "open":
                Contact.open.update(self, value)
            case _:
                super()._measure(key, value)


Driver = Contact  # convention : le chargeur cherche ce nom
