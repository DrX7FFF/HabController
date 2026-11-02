"""Pilote capteur Zigbee de température, humidité et pression."""

from core.base import Attribute, Event, Evt
from drivers.zigbee.sensor import Sensor, SensorReports


class ClimateReports(SensorReports, total=False):
    temperature: float | None  # °C
    humidity: float | None  # %
    pressure: float | None  # hPa


class Climate(Sensor):
    DEFAULTS = {"temperature": 0.1, "humidity": 1, "pressure": None, "battery": 1}
    temperature = Attribute[float](readonly=True)
    humidity = Attribute[float](readonly=True)
    pressure = Attribute[float](readonly=True)
    event_temperature = Evt[Event[float]]()
    event_humidity = Evt[Event[float]]()
    event_pressure = Evt[Event[float]]()

    def __init__(self, ieee: str, reports: ClimateReports | None = None):
        super().__init__(ieee, reports)


Driver = Climate  # convention : le chargeur cherche ce nom
