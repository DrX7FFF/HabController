"""Présence : éclairage du salon et de la cuisine selon la présence globale du FP2."""

from core.automation import cuisine, on_change, salon
import logging
log = logging.getLogger(__name__)

@on_change("fp2.zone.Global")
async def eclairage(event):
    etat = "on" if event.new else "off"
    salon.power = etat
    cuisine.power = etat
    log.warning("Detection = %s", event.new)
