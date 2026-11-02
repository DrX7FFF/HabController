"""Présence : éclairage du salon et de la cuisine selon la présence globale du FP2."""

from core.automation import *
from devices.cuisine import cuisine
from devices.salon import fp2, salon


@fp2.event_zone
async def eclairage(event: EventZone):
    if event.zonename != "Global":
        return
    salon.on = event.new
    cuisine.on = event.new
    log.warning("Detection = %s", event.new)
