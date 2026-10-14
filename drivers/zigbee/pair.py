"""Appairage Zigbee via la clé USB (déroulé commun : core/pair.py).

HabController doit être arrêté (il occupe le port série de la clé).
Le réseau est ouvert pendant `--duration` secondes : mettre l'équipement en mode appairage
(souvent un appui long). Le premier équipement qui termine son interview est retenu ; il est
reconnu par son IEEE (ex. après un changement de clé). La copie de la base
(pairing/zigbee/zigbee.db) est mise à jour.
"""

import asyncio
from pathlib import Path

from core import pair
from drivers import zigbee
from drivers.zigbee import describe, joined_devices, save_pairing, save_snapshot

MAX_PERMIT = 254  # s, limite du protocole pour une ouverture du réseau
INTERVIEW_WAIT = 60  # s, délai laissé à une interview commencée quand le réseau se referme
SETTLE = 10  # s, laissés à l'équipement juste appairé avant de continuer


class PairingListener:
    """Événements du coordinateur pendant l'ouverture du réseau (appelés dans la boucle asyncio)."""

    def __init__(self):
        self.joining: set = set()  # IEEE des équipements en cours d'interview
        self.done: asyncio.Future = asyncio.get_running_loop().create_future()

    def device_joined(self, device) -> None:
        self.joining.add(device.ieee)
        print(f"Équipement {device.ieee} en cours d'association, interview…")

    def device_initialized(self, device) -> None:
        self.joining.discard(device.ieee)
        if not self.done.done():
            self.done.set_result(device)

    def device_init_failure(self, device) -> None:
        self.joining.discard(device.ieee)
        print(f"Interview de {device.ieee} échouée : relancer le mode appairage de l'équipement.")


async def wait_device(app, duration: int):
    """Ouvre le réseau et attend un équipement interviewé ; None si aucun."""
    listener = PairingListener()
    app.add_listener(listener)
    print(f"Réseau ouvert pendant {duration} s : mettre l'équipement en mode appairage.")
    await app.permit(duration)
    try:
        return await asyncio.wait_for(asyncio.shield(listener.done), duration)
    except TimeoutError:
        if not listener.joining:
            return None
        print("Réseau refermé, interview en cours : on patiente encore.")
        try:
            return await asyncio.wait_for(listener.done, INTERVIEW_WAIT)
        except TimeoutError:
            return None
    finally:
        await app.permit(0)  # referme le réseau sans attendre la fin du délai


class Pairing(pair.Pairing):
    PROTOCOL = zigbee
    HELP = "appairer un équipement Zigbee (HabController arrêté : il occupe la clé)"
    DURATION = 120
    MAX_DURATION = MAX_PERMIT
    HINT = "Vérifier le port ([zigbee] de habctrl.toml) et que HabController est arrêté."

    async def search(self, app, duration: int) -> list[pair.Candidate]:
        print(f"Coordinateur démarré, {len(joined_devices(app))} équipement(s) déjà sur le réseau.")
        device = await wait_device(app, duration)
        if device is None:
            return []
        # juste après l'interview, l'équipement échange encore (ex. demande de mise à jour OTA)
        await asyncio.sleep(SETTLE)
        return [
            pair.Candidate(
                uid=device.ieee,
                model=device.model or "",
                comment=f"Zigbee : {device.manufacturer}, modèle {device.model}",
                details=describe(device),
                device=device,
            )
        ]

    async def link(self, app, candidate: pair.Candidate, dev_id: str) -> Path:
        return save_pairing(dev_id, candidate.device)

    def done(self) -> None:
        save_snapshot()  # le réseau a changé : seule écriture de la copie
