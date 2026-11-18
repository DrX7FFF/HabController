"""Pilote détecteur de présence Zigbee."""

from core.base import EventBool, EventInt, EventSource, State, known
from drivers.zigbee.sensor import Sensor, SensorReports


class MotionReports(SensorReports, total=False):
    presence: bool | None
    illuminance: float | None  # % (écart relatif)


class Motion(Sensor):
    DEFAULTS = {"presence": True, "illuminance": 2, "battery": 1}
    presence = State[bool]()
    illuminance = State[int]()  # lx
    event_presence = EventSource[EventBool]()
    event_illuminance = EventSource[EventInt]()

    def __init__(self, ieee: str, reports: MotionReports | None = None):
        super().__init__(ieee, reports)

    def state(self) -> dict:
        state = super().state()
        if not state.get("connected"):
            return state
        return state | known(presence=self.presence, illuminance=self.illuminance)

    def measures(self) -> dict[str, str]:
        return dict.fromkeys(("presence", "illuminance",), "tracked") | super().measures()

    def _reset(self) -> None:
        Motion.presence.update(self, None)
        Motion.illuminance.update(self, None)
        super()._reset()

    def _measure(self, key: str, value) -> None:
        match key:
            case "presence":
                Motion.presence.update(self, value)
            case "illuminance":
                Motion.illuminance.update(self, value)
            case _:
                super()._measure(key, value)


Driver = Motion  # convention : le chargeur cherche ce nom
