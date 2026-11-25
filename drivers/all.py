"""Toutes les classes de pilotes, à importer d'une ligne : `from drivers.all import Plug, Light`."""

from drivers.esp32matrix import RGBMatrix
from drivers.homekit.fp2 import FP2
from drivers.roborock import Roborock
from drivers.samsung import SamsungTV
from drivers.wled import WLED
from drivers.yeelight import YeelightBulb
from drivers.zigbee.button import Button
from drivers.zigbee.climate import Climate
from drivers.zigbee.contact import Contact
from drivers.zigbee.light import Light
from drivers.zigbee.motion import Motion
from drivers.zigbee.plug import Plug

__all__ = [
    "FP2",
    "WLED",
    "Button",
    "Climate",
    "Contact",
    "Light",
    "Motion",
    "Plug",
    "RGBMatrix",
    "Roborock",
    "SamsungTV",
    "YeelightBulb",
]
