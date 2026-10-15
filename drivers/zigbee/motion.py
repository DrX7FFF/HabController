"""Détecteur de présence Zigbee, avec luminosité s'il la mesure (sur pile, base : sensor.py).

État : {"connected": bool, "presence": bool, "illuminance": lx, "battery": %}
Un détecteur qui n'envoie jamais « plus personne » (Xiaomi) est remis à faux par son
correctif zha-quirks après un délai.

    from drivers.zigbee.motion import Motion

    presence_entree = Motion(name="Présence entrée")
"""

from core.base import Attribute
from drivers.zigbee.sensor import Sensor


class Motion(Sensor):
    presence = Attribute[bool](readonly=True)
    illuminance = Attribute[int](readonly=True)


Driver = Motion  # convention : le chargeur cherche ce nom
