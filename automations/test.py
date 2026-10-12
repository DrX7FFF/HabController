"""Présence : éclairage du salon et de la cuisine selon la présence globale du FP2."""

from core.automation import on_change, salon
import logging
log = logging.getLogger(__name__)

@on_change("salon.power")
async def test(event):
    log.debug("salon = %s %s", event.new, salon.brightness)
