"""Pilote capteur Zigbee de température, humidité et pression."""

from core.base import EventFloat, EventSource, State, known
from drivers.zigbee.sensor import Sensor, SensorReports


class ClimateReports(SensorReports, total=False):
    temperature: float | None  # °C
    humidity: float | None  # %
    pressure: float | None  # hPa


class Climate(Sensor):
    DEFAULTS = {"temperature": 0.1, "humidity": 1, "pressure": None, "battery": 1}
    temperature = State[float]()  # °C
    humidity = State[float]()  # %
    pressure = State[float]()  # hPa
    event_temperature = EventSource[EventFloat]()
    event_humidity = EventSource[EventFloat]()
    event_pressure = EventSource[EventFloat]()

    def __init__(self, ieee: str, reports: ClimateReports | None = None):
        super().__init__(ieee, reports)

    def state(self) -> dict:
        state = super().state()
        if not state.get("connected"):
            return state
        return state | known(temperature=self.temperature, humidity=self.humidity, pressure=self.pressure)

    def measures(self) -> dict[str, str]:
        return dict.fromkeys(("temperature", "humidity", "pressure",), "tracked") | super().measures()

    def _reset(self) -> None:
        Climate.temperature.update(self, None)
        Climate.humidity.update(self, None)
        Climate.pressure.update(self, None)
        super()._reset()

    def _measure(self, key: str, value) -> None:
        match key:
            case "temperature":
                Climate.temperature.update(self, value)
            case "humidity":
                Climate.humidity.update(self, value)
            case "pressure":
                Climate.pressure.update(self, value)
            case _:
                super()._measure(key, value)


Driver = Climate  # convention : le chargeur cherche ce nom
