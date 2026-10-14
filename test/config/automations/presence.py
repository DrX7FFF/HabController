"""Présence : éclairage du salon et de la cuisine selon la présence globale du FP2."""

import logging

from core.automation import Event, on_change
from devices.cuisine import cuisine
from devices.salon import salon

log = logging.getLogger(__name__)


@on_change("fp2.zone.Global")
async def eclairage(event: Event):
    salon.on = event.new
    cuisine.on = event.new
    log.warning("Detection = %s", event.new)
