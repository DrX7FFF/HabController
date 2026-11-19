"""Présence : éclairage du salon et de la cuisine selon la présence globale du FP2."""

from devices.cuisine import cuisine
from devices.salon import fp2, salon

from core.automation import *


@fp2.event_zone
async def eclairage(event: EventZone):
    if event.zonename != "Global":
        return
    salon.switch(event.new)
    cuisine.switch(event.new)
