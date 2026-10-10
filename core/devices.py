import importlib
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
