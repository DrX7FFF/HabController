"""Pilote capteur Zigbee de température, humidité et pression."""

from core.base import Attribute
from drivers.zigbee.sensor import Sensor


class Climate(Sensor):
    temperature = Attribute[float](readonly=True)
    humidity = Attribute[float](readonly=True)
    pressure = Attribute[float](readonly=True)


Driver = Climate  # convention : le chargeur cherche ce nom
