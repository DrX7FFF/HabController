"""HomeKit (HAP) : appairage et connexion, communs à tous les pilotes HomeKit.

L'appairage d'un équipement est enregistré dans pairing/homekit/<id>.json (droits 600).
Il est portable : l'adresse IP n'y figure jamais, elle est résolue par mDNS à chaque connexion
à partir de l'identifiant HomeKit de l'accessoire. Copié sur une nouvelle installation, il reste
valable tant que l'accessoire n'a pas été réinitialisé.
"""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from aiohomekit import Controller
from aiohomekit.exceptions import HomeKitException, HttpException
from aiohomekit.zeroconf import (
    HAP_TYPE_TCP,
    HAP_TYPE_UDP,
    HomeKitService,
    ZeroconfServiceListener,
)
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

from core import PAIRING_DIR as CONFIG_PAIRING_DIR

ERRORS = (HomeKitException, HttpException, OSError)  # erreurs du protocole ou du réseau
PAIRING_DIR = CONFIG_PAIRING_DIR / "homekit"
PIN = re.compile(r"\d{3}-\d{2}-\d{3}")
ENDPOINT_KEYS = ("AccessoryIP", "AccessoryIPs", "AccessoryPort", "AccessoryAddress")


class PairingError(Exception):
    """Appairage absent ou illisible pour cet équipement."""


def pairing_file(dev_id: str) -> Path:
    if not re.fullmatch(r"[\w-]+", dev_id):
        raise ValueError(f"identifiant invalide « {dev_id} »")
    return PAIRING_DIR / f"{dev_id}.json"


def load_pairing(dev_id: str) -> dict:
    path = pairing_file(dev_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise PairingError(
            f"appairage absent ({path}) : lancer ./habctrl pair homekit {dev_id}"
        ) from err
    except json.JSONDecodeError as err:
        raise PairingError(f"fichier d'appairage illisible ({path})") from err
    if not isinstance(data, dict) or "AccessoryPairingID" not in data:
        raise PairingError(f"fichier d'appairage invalide ({path})")
    return data


def save_pairing(dev_id: str, data: dict) -> Path:
    path = pairing_file(dev_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out, indent=2)
    tmp.replace(path)
    return path


@asynccontextmanager
async def open_controller():
    """Contrôleur HomeKit avec sa découverte mDNS, fermés à la sortie."""
    async with AsyncZeroconf() as zeroconf:
        # aiohomekit exige un navigateur mDNS déjà lancé : à créer avant le contrôleur
        browser = AsyncServiceBrowser(
            zeroconf.zeroconf, [HAP_TYPE_TCP, HAP_TYPE_UDP], listener=ZeroconfServiceListener()
        )
        try:
            async with Controller(async_zeroconf_instance=zeroconf) as controller:
                yield controller
        finally:
            await browser.async_cancel()


async def discover(controller: Controller, listen: float = 10.0) -> list:
    """Accessoires HomeKit vus sur le réseau après `listen` secondes d'écoute."""
    await asyncio.sleep(listen)
    return [discovery async for discovery in controller.async_discover()]


async def pair_accessory(discovery, dev_id: str, pin: str) -> Path:
    """Appaire l'accessoire et enregistre l'appairage (sans IP). Le code n'est jamais journalisé."""
    if not PIN.fullmatch(pin):
        raise ValueError("code attendu au format XXX-XX-XXX")
    finish = await discovery.async_start_pairing(dev_id)
    pairing = await finish(pin)
    try:
        data = {
            key: value for key, value in pairing.pairing_data.items() if key not in ENDPOINT_KEYS
        }
        return save_pairing(dev_id, data)
    finally:
        await pairing.close()


async def connect(controller: Controller, dev_id: str, wait: float = 30.0):
    """Charge l'appairage de l'équipement ; l'adresse vient du mDNS, jamais du fichier."""
    data = load_pairing(dev_id)
    discovery = await controller.async_find(data["AccessoryPairingID"], wait)
    description = discovery.description
    if not isinstance(description, HomeKitService):  # IP uniquement (pas BLE/CoAP)
        raise PairingError(f"{dev_id} : accessoire non IP, non pris en charge")
    data = {
        **data,
        "AccessoryIP": description.address,
        "AccessoryIPs": description.addresses,
        "AccessoryPort": description.port,
    }
    return controller.load_pairing(dev_id, data)
