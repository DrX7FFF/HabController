"""Pilote contact d'ouverture Zigbee."""

from core.base import Attribute
from drivers.zigbee.sensor import Sensor


class Contact(Sensor):
    open = Attribute[bool](readonly=True)


Driver = Contact  # convention : le chargeur cherche ce nom
