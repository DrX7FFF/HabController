import importlib
import json
import keyword
import pkgutil
from pathlib import Path

from core import ROOT
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
    modules = pkgutil.iter_modules(importlib.import_module(package).__path__)
    for info in sorted(modules, key=lambda module: module.name):
        dev_id = info.name
        if dev_id.startswith("_"):
            continue
        device = getattr(importlib.import_module(f"{package}.{dev_id}"), dev_id, None)
        if not isinstance(device, Device):
            raise ValueError(f"{package}/{dev_id}.py doit définir `{dev_id} = <pilote>(...)`")
        device.configure(dev_id)
        devices[dev_id] = device
    return devices


def check_new_device(dev_id: str, driver: str = "", path: Path = ROOT / "devices") -> None:
    """Vérifie qu'on peut déclarer cet équipement ; lève ValueError sinon."""
    if not dev_id.isidentifier() or keyword.iskeyword(dev_id) or dev_id.startswith("_"):
        raise ValueError(
            f"identifiant invalide « {dev_id} » (nom de variable Python, sans « _ » initial)"
        )
    if (path / f"{dev_id}.py").exists():
        raise ValueError(f"l'équipement « {dev_id} » existe déjà dans {path.name}")
    if driver:
        load_driver(driver)


def add_device(
    dev_id: str,
    driver: str,
    name: str = "",
    comment: str = "",
    path: Path = ROOT / "devices",
) -> None:
    """Crée devices/<id>.py : `<id> = <Pilote>(...)`, avec les réglages par défaut du pilote."""
    check_new_device(dev_id, driver, path)
    if not driver:
        raise ValueError("pilote requis")
    cls = load_driver(driver)
    args = [f"name={json.dumps(name, ensure_ascii=False)}"] if name else []
    config_args = getattr(cls, "config_args", None)
    if config_args:
        args += config_args()
    lines = [f"# {comment}"] if comment else []
    lines += [
        f"from drivers.{driver} import {cls.__name__}",
        "",
        f"{dev_id} = {cls.__name__}({', '.join(args)})",
    ]
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{dev_id}.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
