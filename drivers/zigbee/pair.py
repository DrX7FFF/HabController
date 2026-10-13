"""Appaire un équipement Zigbee via la clé USB et crée devices/<id>.py.

Usage : ./habctrl pair zigbee <id> [--duration 120]
HabController doit être arrêté (il occupe le port série de la clé).
Le réseau est ouvert pendant `--duration` secondes : mettre l'équipement en mode appairage
(souvent un appui long). Le premier équipement qui termine son interview est retenu.
Son pilote est déduit du modèle (drivers/zigbee/models.toml), sinon aucun fichier n'est créé :
l'équipement est à déclarer à la main dans devices/ avec l'adresse IEEE affichée.
Redémarrer HabController ensuite.
"""

import argparse
import asyncio
from pathlib import Path

from core.devices import add_device, check_new_device, driver_for_model
from drivers.zigbee import ERRORS, describe, joined_devices, open_coordinator

HELP = "appairer un équipement Zigbee (HabController arrêté : il occupe la clé)"
MODELS = Path(__file__).with_name("models.toml")
MAX_PERMIT = 254  # s, limite du protocole pour une ouverture du réseau
INTERVIEW_WAIT = 60  # s, délai laissé à une interview commencée quand le réseau se referme
SETTLE = 10  # s, avant de fermer la clé une fois l'équipement appairé


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


async def run(args: argparse.Namespace) -> int:
    try:
        check_new_device(args.id)
    except ValueError as err:
        print(f"Erreur : {err}")
        return 1

    try:
        async with open_coordinator() as app:
            known = {device.ieee for device in joined_devices(app)}
            print(f"Coordinateur démarré, {len(known)} équipement(s) déjà sur le réseau.")
            device = await wait_device(app, args.duration)
            if device is not None:
                # juste après l'interview, l'équipement échange encore (ex. demande de mise à jour
                # OTA) : fermer la clé tout de suite couperait la réponse en plein envoi
                await asyncio.sleep(SETTLE)
    except ERRORS as err:
        print(f"Clé Zigbee inaccessible ({type(err).__name__} : {err}).")
        print("Vérifier le port ([zigbee] de habctrl.toml) et que HabController est arrêté.")
        return 1

    if device is None:
        print("Aucun équipement appairé.")
        return 1
    print()
    print("\n".join(describe(device)))
    if device.ieee in known:
        print("Équipement déjà connu du réseau : réappairé.")

    ieee_arg = f'ieee="{device.ieee}"'
    driver = driver_for_model(device.model or "", MODELS)
    if not driver:
        print(
            f"\nModèle inconnu : déclarer l'équipement dans devices/{args.id}.py avec le bon "
            f"pilote et {ieee_arg}, ou ajouter le modèle à drivers/zigbee/models.toml."
        )
        return 0
    try:
        add_device(
            args.id,
            driver,
            comment=f"Zigbee : {device.manufacturer}, modèle {device.model}",
            args=[ieee_arg],
        )
    except ValueError as err:
        print(f"Équipement non ajouté : {err}")
        return 1
    print(f"Équipement « {args.id} » créé : devices/{args.id}.py.")
    return 0


def duration(text: str) -> int:
    value = int(text)
    if not 1 <= value <= MAX_PERMIT:
        raise argparse.ArgumentTypeError(f"entre 1 et {MAX_PERMIT} secondes")
    return value


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--duration", type=duration, default=120, help="ouverture du réseau en secondes"
    )
