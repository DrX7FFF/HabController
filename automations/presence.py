"""Présence : éclairage du salon et de la cuisine selon le capteur de présence 0 du FP2."""

import logging

from core.automation import devices, on_change
from core.base import DeviceError

log = logging.getLogger(__name__)

AMPOULES = ("salon", "cuisine")


@on_change("fp2.presence_0")
async def eclairage(event):
    etat = "on" if event.new else "off"
    for ampoule in AMPOULES:
        try:
            await devices[ampoule].set("power", etat)
        except DeviceError as err:  # une ampoule injoignable n'empêche pas l'autre
            log.warning("%s : %s", ampoule, err)
