"""Contact d'ouverture Zigbee (porte, fenêtre ; sur pile, base : sensor.py).

État : {"connected": bool, "open": bool, "battery": %}

    from drivers.zigbee.contact import Contact

    porte_entree = Contact()
"""

from core.base import Attribute
from drivers.zigbee.sensor import Sensor


class Contact(Sensor):
    open = Attribute[bool](readonly=True)


Driver = Contact  # convention : le chargeur cherche ce nom
