"""HomeKit : appairage et connexion, communs aux pilotes HomeKit."""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from aiohomekit import Controller
from aiohomekit.exceptions import HomeKitException, HttpException
from aiohomekit.model import Accessories
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


class PairingError(Exception):
    """Appairage absent ou illisible pour cet équipement."""


def pairing_file(dev_id: str) -> Path:
    if not re.fullmatch(r"[\w-]+", dev_id):
        raise ValueError(f"identifiant invalide « {dev_id} »")
    return PAIRING_DIR / f"{dev_id}.json"


def read_pairing_file(dev_id: str) -> dict:
    """Fichier d'appairage : {"hap": clés d'aiohomekit, "info": ce qu'annonce l'accessoire}."""
    path = pairing_file(dev_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise PairingError(f"appairage absent ({path}) : l'appairer") from err
    except json.JSONDecodeError as err:
        raise PairingError(f"fichier d'appairage illisible ({path})") from err
    if not isinstance(data.get("hap"), dict) or "AccessoryPairingID" not in data["hap"]:
        raise PairingError(f"fichier d'appairage invalide ({path})")
    return data


def load_pairing(dev_id: str) -> dict:
    """Clés d'appairage aiohomekit de l'équipement."""
    return read_pairing_file(dev_id)["hap"]


def pairings() -> dict[str, dict]:
    """Fichiers d'appairage lisibles de pairing/homekit/ : {id: contenu}."""
    found = {}
    for path in sorted(PAIRING_DIR.glob("*.json")):
        try:
            found[path.stem] = read_pairing_file(path.stem)
        except (PairingError, ValueError):
            continue
    return found


def paired_ids(accessory_id: str) -> list[str]:
    """Équipements logiques déjà rattachés à cet accessoire (identifiant HomeKit, ex. A8:01:…)."""
    return [i for i, d in pairings().items()
            if d["hap"]["AccessoryPairingID"].lower() == accessory_id]


def links() -> dict[str, str]:
    """Accessoires appairés : {id: description}."""
    return {i: f"HomeKit {d['hap']['AccessoryPairingID']} "
               f"({d['info'].get('Manufacturer', '?')} {d['info'].get('Model', '?')})"
            for i, d in pairings().items()}


def link_models() -> dict[str, tuple[list[str], str]]:
    """Accessoires appairés : {id: (clés de models.toml, commentaire de déclaration)}."""
    return {i: ([d["info"].get("Model", "")],
                f"HomeKit : {d['info'].get('Name', '?')}, modèle {d['info'].get('Model') or '?'}")
            for i, d in pairings().items()}


def save_pairing(dev_id: str, data: dict) -> Path:
    """Écrit le fichier d'appairage (voir read_pairing_file), lisible du seul propriétaire."""
    path = pairing_file(dev_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out, indent=2, ensure_ascii=False)
    return path


async def accessory_info(pairing) -> dict:
    """Ce que l'accessoire annonce dans son service « Accessory Information »."""
    accessories = Accessories.from_list(await pairing.list_accessories_and_characteristics())
    service = accessories.aid(1).accessory_information  # l'accessoire principal
    return {c.description: c.value for c in service.characteristics
            if c.description and c.value is not None}


@asynccontextmanager
async def open_session():
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
    """Appaire l'accessoire et enregistre l'appairage. Le code n'est jamais journalisé."""
    if not PIN.fullmatch(pin):
        raise ValueError("code attendu au format XXX-XX-XXX")
    finish = await discovery.async_start_pairing(dev_id)
    pairing = await finish(pin)
    try:
        try:
            info = await accessory_info(pairing)
        except ERRORS:
            info = {}  # facultatif : l'appairage reste valable sans
        return save_pairing(dev_id, {"hap": pairing.pairing_data, "info": info})
    finally:
        await pairing.close()


async def connect(controller: Controller, dev_id: str, wait: float = 30.0):
    """Charge l'appairage de l'équipement ; l'adresse vient du mDNS, pas du fichier."""
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
