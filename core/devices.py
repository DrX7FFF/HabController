import importlib
import json
import logging
import re
import tomllib
from pathlib import Path

from core import CONFIG_DIR

log = logging.getLogger(__name__)


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
        driver_name = params.pop("driver", "")
        if not driver_name:  # équipement déclaré (ex. par pair.py) mais pas encore configuré
            log.warning("%s : pilote à renseigner dans %s, équipement ignoré", dev_id, path.name)
            continue
        if not params.get("name"):
            params["name"] = dev_id
        devices[dev_id] = load_driver(driver_name)(id=dev_id, **params)
    return devices


def check_new_device(
    dev_id: str, driver: str = "", path: Path = CONFIG_DIR / "devices.toml"
) -> None:
    """Vérifie qu'on peut déclarer cet équipement ; lève ValueError sinon."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", dev_id):
        raise ValueError(f"identifiant invalide « {dev_id} » (lettres, chiffres, _, - seulement)")
    if dev_id in tomllib.loads(path.read_text(encoding="utf-8")):
        raise ValueError(f"l'équipement « {dev_id} » existe déjà dans {path.name}")
    if driver:
        load_driver(driver)


def add_device(
    dev_id: str,
    driver: str = "",
    name: str = "",
    comment: str = "",
    path: Path = CONFIG_DIR / "devices.toml",
) -> None:
    """Ajoute la section de l'équipement en fin de devices.toml (commentaires conservés).

    Le pilote et le nom vides sont marqués « à renseigner » ; load_devices ignore l'équipement
    tant que le pilote n'est pas renseigné.
    """
    check_new_device(dev_id, driver, path)
    todo = "  # à renseigner"
    lines = [f"# {comment}"] if comment else []
    lines += [
        f"[{dev_id}]",
        f"name = {json.dumps(name, ensure_ascii=False)}" + ("" if name else todo),
        f"driver = {json.dumps(driver)}" + ("" if driver else todo),
    ]
    text = path.read_text(encoding="utf-8")
    separator = "" if text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
    path.write_text(text + separator + "\n".join(lines) + "\n", encoding="utf-8")
