import importlib
import json
import re
import tomllib
from pathlib import Path

from core import CONFIG_DIR


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


def load_devices(path: Path = CONFIG_DIR / "devices.toml") -> dict:
    conf = tomllib.loads(path.read_text(encoding="utf-8"))
    devices = {}
    for dev_id, params in conf.items():
        params = dict(params)
        driver = load_driver(params.pop("driver"))
        devices[dev_id] = driver(id=dev_id, **params)
    return devices


def check_new_device(
    dev_id: str, driver: str, path: Path = CONFIG_DIR / "devices.toml"
) -> None:
    """Vérifie qu'on peut déclarer cet équipement ; lève ValueError sinon."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", dev_id):
        raise ValueError(f"identifiant invalide « {dev_id} » (lettres, chiffres, _, - seulement)")
    if dev_id in tomllib.loads(path.read_text(encoding="utf-8")):
        raise ValueError(f"l'équipement « {dev_id} » existe déjà dans {path.name}")
    load_driver(driver)


def add_device(
    dev_id: str, driver: str, name: str, path: Path = CONFIG_DIR / "devices.toml"
) -> None:
    """Ajoute la section de l'équipement en fin de devices.toml (commentaires conservés)."""
    check_new_device(dev_id, driver, path)
    text = path.read_text(encoding="utf-8")
    section = f"[{dev_id}]\nname = {json.dumps(name, ensure_ascii=False)}\ndriver = \"{driver}\"\n"
    separator = "" if text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
    path.write_text(text + separator + section, encoding="utf-8")
