"""Capteur Zigbee de température, humidité et pression (sur pile, base : sensor.py).

État : {"connected": bool, "temperature": °C, "humidity": %, "pressure": hPa, "battery": %}
Seules les mesures que l'appareil envoie apparaissent.

    from drivers.zigbee.climate import Climate

    meteo_salon = Climate(name="Météo salon")
"""

from core.base import Attribute
from drivers.zigbee.sensor import Sensor


class Climate(Sensor):
    temperature = Attribute[float](readonly=True)
    humidity = Attribute[float](readonly=True)
    pressure = Attribute[float](readonly=True)


Driver = Climate  # convention : le chargeur cherche ce nom
