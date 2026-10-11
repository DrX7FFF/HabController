"""Présence : éclairage du salon et de la cuisine selon le capteur de présence 0 du FP2."""

from core.automation import devices, on_change

AMPOULES = ("salon", "cuisine")


@on_change("fp2.presence_0")
async def eclairage(event):
    etat = "on" if event.new else "off"
    for ampoule in AMPOULES:
        await devices[ampoule].set("power", etat)
