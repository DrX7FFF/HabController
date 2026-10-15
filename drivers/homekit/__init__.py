"""HomeKit (HAP) : appairage et connexion, communs à tous les pilotes HomeKit.

L'appairage d'un équipement est enregistré dans pairing/homekit/<id>.toml (droits 600) : lien
entre l'équipement logique (déclaré dans devices/) et l'accessoire physique. En tête, ce qui le
décrit (ce que l'accessoire annonce : fabricant, modèle, n° de série, firmware) ; dans [hap],
les clés d'appairage d'aiohomekit, à ne pas modifier.
Il est portable : l'adresse IP n'y figure jamais, elle est résolue par mDNS à chaque connexion
à partir de l'identifiant HomeKit de l'accessoire. Copié sur une nouvelle installation, il reste
valable tant que l'accessoire n'a pas été réinitialisé.
"""

import asyncio
import json
import os
import re
import tomllib
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from aiohomekit import Controller
from aiohomekit.exceptions import HomeKitException, HttpException
from aiohomekit.model.characteristics import CharacteristicsTypes as Chars
from aiohomekit.model.services import ServicesTypes
from aiohomekit.uuid import normalize_uuid
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
# Service « Accessory Information » : caractéristique -> champ du fichier d'appairage
INFO_CHARS = {
    normalize_uuid(Chars.NAME): "name",
    normalize_uuid(Chars.MANUFACTURER): "manufacturer",
    normalize_uuid(Chars.MODEL): "model",
    normalize_uuid(Chars.SERIAL_NUMBER): "serial",
    normalize_uuid(Chars.FIRMWARE_REVISION): "firmware",
    normalize_uuid(Chars.HARDWARE_REVISION): "hardware",
}


class PairingError(Exception):
    """Appairage absent ou illisible pour cet équipement."""


def pairing_file(dev_id: str) -> Path:
    if not re.fullmatch(r"[\w-]+", dev_id):
        raise ValueError(f"identifiant invalide « {dev_id} »")
    return PAIRING_DIR / f"{dev_id}.toml"


def read_pairing_file(dev_id: str) -> dict:
    """Fichier d'appairage complet : informations, et clés aiohomekit sous « hap »."""
    path = pairing_file(dev_id)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise PairingError(
            f"appairage absent ({path}) : lancer ./habctrl pair homekit"
        ) from err
    except tomllib.TOMLDecodeError as err:
        raise PairingError(f"fichier d'appairage illisible ({path})") from err
    if not isinstance(data.get("hap"), dict) or "AccessoryPairingID" not in data["hap"]:
        raise PairingError(f"fichier d'appairage invalide ({path})")
    return data


def load_pairing(dev_id: str) -> dict:
    """Clés d'appairage aiohomekit de l'équipement."""
    return read_pairing_file(dev_id)["hap"]


def paired_ids(accessory_id: str) -> list[str]:
    """Équipements logiques déjà rattachés à cet accessoire (identifiant HomeKit, ex. A8:01:…)."""
    found = []
    for path in sorted(PAIRING_DIR.glob("*.toml")):
        try:
            if read_pairing_file(path.stem)["hap"]["AccessoryPairingID"].lower() == accessory_id:
                found.append(path.stem)
        except (PairingError, ValueError):
            continue
    return found


def links() -> dict[str, str]:
    """Accessoires appairés : {id: description}, d'après pairing/homekit/."""
    found = {}
    for path in sorted(PAIRING_DIR.glob("*.toml")):
        try:
            data = read_pairing_file(path.stem)
        except (PairingError, ValueError):
            continue
        found[path.stem] = (
            f"HomeKit {data['hap']['AccessoryPairingID']}"
            f" ({data.get('manufacturer', '?')} {data.get('model', '?')})"
        )
    return found



def link_models() -> dict[str, tuple[list[str], str]]:
    """Accessoires appairés : {id: (clés de models.toml, commentaire de déclaration)}."""
    found = {}
    for path in sorted(PAIRING_DIR.glob("*.toml")):
        try:
            data = read_pairing_file(path.stem)
        except (PairingError, ValueError):
            continue
        model = data.get("model", "")
        found[path.stem] = ([model], f"HomeKit : {data.get('name', '?')}, modèle {model or '?'}")
    return found


def save_pairing(dev_id: str, keys: dict, info: dict | None = None) -> Path:
    """Écrit l'appairage : `info` (champs de INFO_CHARS, et « paired ») puis les clés `keys`."""
    info = info or {}
    lines = [
        f"# Appairage HomeKit de « {dev_id} » : lien entre l'équipement logique",
        "# (déclaré dans devices/) et l'accessoire physique. Écrit par ./habctrl pair homekit.",
        "",
    ]
    lines += [
        f"{field} = {json.dumps(str(info[field]), ensure_ascii=False)}"
        for field in INFO_CHARS.values()
        if info.get(field)
    ]
    if isinstance(paired := info.get("paired"), datetime):
        lines.append(f"paired = {paired.replace(microsecond=0).isoformat()}")
    lines += ["", "[hap]  # clés d'appairage d'aiohomekit : ne pas modifier"]
    lines += [f"{key} = {json.dumps(value)}" for key, value in keys.items()]
    path = pairing_file(dev_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write("\n".join(lines) + "\n")
    tmp.replace(path)
    return path


async def accessory_info(pairing) -> dict:
    """Ce que l'accessoire annonce dans son service « Accessory Information »."""
    info = {}
    for accessory in await pairing.list_accessories_and_characteristics():
        if accessory.get("aid") != 1:  # l'accessoire principal (les autres : passerelle)
            continue
        for service in accessory.get("services", []):
            if normalize_uuid(str(service.get("type", ""))) != normalize_uuid(
                ServicesTypes.ACCESSORY_INFORMATION
            ):
                continue
            for char in service.get("characteristics", []):
                field = INFO_CHARS.get(normalize_uuid(str(char.get("type", ""))))
                if field and char.get("value") not in (None, ""):
                    info[field] = char["value"]
    return info


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
    """Appaire l'accessoire et enregistre l'appairage (sans IP). Le code n'est jamais journalisé."""
    if not PIN.fullmatch(pin):
        raise ValueError("code attendu au format XXX-XX-XXX")
    finish = await discovery.async_start_pairing(dev_id)
    pairing = await finish(pin)
    try:
        keys = {
            key: value for key, value in pairing.pairing_data.items() if key not in ENDPOINT_KEYS
        }
        save_pairing(dev_id, keys)  # d'abord les clés : l'appairage ne doit pas se perdre
        try:
            info = await accessory_info(pairing)
        except ERRORS:
            info = {}  # facultatif : l'appairage reste valable sans
        return save_pairing(dev_id, keys, {**info, "paired": datetime.now()})
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
