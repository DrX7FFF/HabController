"""Pilote capteur Zigbee de température, humidité et pression."""

from core.base import EventFloat, EventSource, State
from drivers.zigbee.sensor import Sensor, SensorReports


class ClimateReports(SensorReports, total=False):
    temperature: float | None  # °C
    humidity: float | None  # %
    pressure: float | None  # hPa


DEFAULTS: ClimateReports = {"temperature": 0.1, "humidity": 1, "pressure": None, "battery": 1}


class Climate(Sensor):
    DEFAULTS = DEFAULTS
    temperature = State[float]()  # °C
    humidity = State[float]()  # %
    pressure = State[float]()  # hPa
    event_temperature = EventSource[EventFloat]()
    event_humidity = EventSource[EventFloat]()
    event_pressure = EventSource[EventFloat]()

    def __init__(self, ieee: str, reports: ClimateReports | None = None):
        super().__init__(ieee, reports)


Driver = Climate  # convention : le chargeur cherche ce nom
