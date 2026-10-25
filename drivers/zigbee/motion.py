"""Pilote détecteur de présence Zigbee."""

from core.base import Attribute
from drivers.zigbee.sensor import Sensor


class Motion(Sensor):
    presence = Attribute[bool](readonly=True)
    illuminance = Attribute[int](readonly=True)


Driver = Motion  # convention : le chargeur cherche ce nom
