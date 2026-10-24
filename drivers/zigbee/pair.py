"""Appairage Zigbee via la clé USB (déroulé commun : core/pair.py).

Utilise la clé de HabController (coordinateur partagé, ouvert pour l'occasion s'il n'y a encore
aucun équipement Zigbee). Le réseau est ouvert pendant la durée choisie : mettre l'équipement
en mode appairage (souvent un appui long). Le premier équipement qui termine son interview est retenu ; il est
reconnu par son IEEE. Le lien est une ligne de pairing/zigbee.toml ; la copie de la base
(pairing/zigbee.db) est mise à jour.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

from core import pair
from drivers import zigbee
from drivers.zigbee import (
    ERRORS,
    PairingError,
    coordinator,
    describe,
    find_cluster,
    load_mapping,
    model_keys,
    read,
    save_mapping,
    save_snapshot,
)
from drivers.zigbee.light import CAPABILITIES, COLOR

MAX_PERMIT = 254  # s, limite du protocole pour une ouverture du réseau
INTERVIEW_WAIT = 60  # s, délai laissé à une interview commencée quand le réseau se referme
SETTLE = 10  # s, laissés à l'équipement juste appairé avant de continuer


class PairingListener:
    """Événements du coordinateur pendant l'ouverture du réseau (appelés dans la boucle asyncio)."""

    def __init__(self, say: Callable[[str], None]):
        self.say = say
        self.joining: set = set()  # IEEE des équipements en cours d'interview
        self.done: asyncio.Future = asyncio.get_running_loop().create_future()

    def device_joined(self, device) -> None:
        self.joining.add(device.ieee)
        self.say(f"Équipement {device.ieee} en cours d'association, interview…")

    def device_initialized(self, device) -> None:
        self.joining.discard(device.ieee)
        if not self.done.done():
            self.done.set_result(device)

    def device_init_failure(self, device) -> None:
        self.joining.discard(device.ieee)
        self.say(f"Interview de {device.ieee} échouée : relancer le mode appairage.")


async def wait_device(app, duration: int, ui: pair.Dialog):
    """Ouvre le réseau et attend un équipement interviewé ; None si aucun."""
    say = ui.say
    listener = PairingListener(say)
    app.add_listener(listener)
    say("Appairage en attente…")
    await app.permit(duration)
    ui.wait(duration)
    try:
        return await asyncio.wait_for(asyncio.shield(listener.done), duration)
    except TimeoutError:
        if not listener.joining:
            return None
        say("Réseau refermé, interview en cours : on patiente encore.")
        try:
            return await asyncio.wait_for(listener.done, INTERVIEW_WAIT)
        except TimeoutError:
            return None
    finally:
        ui.wait(0)
        app.remove_listener(listener)  # la clé reste ouverte : ne pas cumuler les écouteurs
        await app.permit(0)  # referme le réseau sans attendre la fin du délai


async def read_fixed(device, say: Callable[[str], None]) -> None:
    """Lit ce qui ne change jamais et que les pilotes retrouvent ensuite dans la base zigpy (cache
    des attributs, copié avec elle), sans échange radio : les réglages de couleur d'une ampoule."""
    if (cluster := find_cluster(device, COLOR)) is None:
        return
    try:
        await read(cluster, [CAPABILITIES])
    except ERRORS as err:
        say(f"Réglages de couleur non lus ({type(err).__name__}) : tous supposés présents.")


class Pairing(pair.Pairing):
    PROTOCOL = zigbee
    LABEL = "Zigbee"
    DURATION = 120
    MAX_DURATION = MAX_PERMIT
    HINT = "Vérifier le port ([zigbee] de habctrl.toml) et le journal."
    devices: dict = {}  # appareils de la base, pour commenter pairing/zigbee.toml

    def session(self):
        return coordinator.session()

    async def search(self, app, duration: int) -> list[pair.Candidate]:
        try:
            load_mapping(missing_ok=True)
        except PairingError as err:  # on n'écrase pas un fichier à corriger à la main
            self.ui.say(f"Erreur : {err}")
            return []
        self.devices = app.devices
        device = await wait_device(app, duration, self.ui)
        if device is None:
            return []
        # juste après l'interview, l'équipement échange encore (ex. demande de mise à jour OTA)
        await asyncio.sleep(SETTLE)
        await read_fixed(device, self.ui.say)
        return [
            pair.Candidate(
                uid=device.ieee,
                models=model_keys(device),
                comment=f"Zigbee : {device.manufacturer}, modèle {device.model}",
                details=describe(device),
                device=device,
            )
        ]

    async def link(self, app, candidate: pair.Candidate, dev_id: str) -> Path:
        mapping = load_mapping(missing_ok=True)
        mapping = {ieee: linked for ieee, linked in mapping.items() if linked != dev_id}
        mapping[candidate.uid] = dev_id
        return save_mapping(mapping, self.devices)

    def paired_ids(self, uid) -> list[str]:
        dev_id = load_mapping(missing_ok=True).get(uid)
        return [dev_id] if dev_id else []

    def linked(self, dev_id: str) -> bool:
        return dev_id in load_mapping(missing_ok=True).values()

    def unlink(self, dev_id: str) -> None:
        mapping = load_mapping(missing_ok=True)
        if dev_id in mapping.values():
            kept = {ieee: linked for ieee, linked in mapping.items() if linked != dev_id}
            save_mapping(kept, self.devices)

    def done(self) -> None:
        save_snapshot()  # le réseau a changé : seule écriture de la copie
