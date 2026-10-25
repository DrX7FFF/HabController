"""Toutes les classes de pilotes, à importer d'une ligne : `from drivers.all import Plug, Light`."""

from drivers.homekit.fp2 import FP2
from drivers.samsung.tv import SamsungTV
from drivers.yeelight.bulb import YeelightBulb
from drivers.zigbee.button import Button
from drivers.zigbee.climate import Climate
from drivers.zigbee.contact import Contact
from drivers.zigbee.light import Light
from drivers.zigbee.motion import Motion
from drivers.zigbee.plug import Plug
from drivers.zigbee.sensor import Sensor

__all__ = [
    "FP2",
    "SamsungTV",
    "YeelightBulb",
    "Button",
    "Climate",
    "Contact",
    "Light",
    "Motion",
    "Plug",
    "Sensor",
]
