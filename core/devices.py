import importlib
import json
import pkgutil
import re
from pathlib import Path

from core import CONFIG_DIR
from core.base import Device


def load_driver(name: str):
    """Charge le pilote `drivers/<name>.py` : il doit exposer sa classe sous le nom `Driver`."""
    if not name.isidentifier():
        raise ValueError(f"nom de pilote invalide « {name} »")
    try:
        module = importlib.import_module(f"drivers.{name}")
    except ModuleNotFoundError as err:
        if err.name == f"drivers.{name}":  # le fichier du pilote n'existe pas
            raise ValueError(f"pilote inconnu « {name} » (drivers/{name}.py introuvable)") from err
        raise  # dépendance manquante à l'intérieur du pilote : on garde le message d'origine
    if not hasattr(module, "Driver"):
        raise ValueError(f"drivers/{name}.py doit exposer sa classe sous le nom « Driver »")
    return module.Driver


def load_devices(package: str = "devices") -> dict:
    """Importe chaque `devices/<id>.py` : il doit définir une variable `<id>` (instance de Device)."""
    devices = {}
    for info in sorted(pkgutil.iter_modules(importlib.import_module(package).__path__)):
        dev_id = info.name
        if dev_id.startswith("_"):
            continue
        device = getattr(importlib.import_module(f"{package}.{dev_id}"), dev_id, None)
        if not isinstance(device, Device):
            raise ValueError(f"{package}/{dev_id}.py doit définir `{dev_id} = <pilote>(...)`")
        device.configure(dev_id)
        devices[dev_id] = device
    return devices


def check_new_device(
    dev_id: str, driver: str = "", path: Path = CONFIG_DIR / "devices"
) -> None:
    """Vérifie qu'on peut déclarer cet équipement ; lève ValueError sinon."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", dev_id):
        raise ValueError(f"identifiant invalide « {dev_id} » (lettres, chiffres, _, - seulement)")
    if (path / f"{dev_id}.toml").exists():
        raise ValueError(f"l'équipement « {dev_id} » existe déjà dans {path.name}")
    if driver:
        load_driver(driver)


def add_device(
    dev_id: str,
    driver: str = "",
    name: str = "",
    comment: str = "",
    path: Path = CONFIG_DIR / "devices",
) -> None:
    """Crée un fichier TOML pour l'équipement dans le dossier devices.

    Le pilote et le nom vides sont marqués « à renseigner » ; load_devices ignore l'équipement
    tant que le pilote n'est pas renseigné.
    """
    check_new_device(dev_id, driver, path)
    todo = "  # à renseigner"
    lines = [f"# {comment}"] if comment else []
    lines += [
        f"name = {json.dumps(name, ensure_ascii=False)}" + ("" if name else todo),
        f"driver = {json.dumps(driver)}" + ("" if driver else todo),
    ]
    if driver:
        config_lines = getattr(load_driver(driver), "config_lines", None)
        if config_lines:
            lines += config_lines()
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{dev_id}.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
