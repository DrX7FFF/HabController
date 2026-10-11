"""Présence : éclairage du salon et de la cuisine selon la présence globale du FP2."""

from core.automation import devices, log, on_change
from drivers import fp2

AMPOULES = ("salon", "cuisine")


@on_change("fp2.zoneGlobal")
async def eclairage(event):
    log.warning("Luminosité %s", devices["fp2"].state().get("illuminance"))
    etat = "on" if event.new else "off"
    for ampoule in AMPOULES:
        await devices[ampoule].set("power", etat)
