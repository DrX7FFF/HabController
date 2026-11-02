"""Présence : éclairage du salon et de la cuisine selon la présence globale du FP2."""

import logging

from core.base import ZoneChange
from devices.cuisine import cuisine
from devices.salon import fp2, salon

log = logging.getLogger(__name__)


@fp2.evt_zone
async def eclairage(event: ZoneChange):
    if event.zone != "Global":
        return
    salon.on = event.new
    cuisine.on = event.new
    log.warning("Detection = %s", event.new)
