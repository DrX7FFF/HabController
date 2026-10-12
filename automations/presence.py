"""Présence : éclairage du salon et de la cuisine selon la présence globale du FP2."""

from core.automation import cuisine, on_change, salon


@on_change("fp2.zoneGlobal")
async def eclairage(event):
    etat = "on" if event.new else "off"
    salon.power = etat
    cuisine.power = etat
