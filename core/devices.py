import importlib
import json
import keyword
import pkgutil
import re
import tomllib
import unicodedata
from pathlib import Path

from core import CONFIG_DIR
from core.base import Device

DEVICES_DIR = CONFIG_DIR / "devices"


def load_driver(name: str):
    """Charge le pilote `drivers/<name>.py` : il doit exposer sa classe sous le nom `Driver`.

    Un pilote rangé dans un sous-dossier se nomme avec un point : "zigbee.plug".
    """
    if not all(part.isidentifier() for part in name.split(".")):
        raise ValueError(f"nom de pilote invalide « {name} »")
    file = f"drivers/{name.replace('.', '/')}.py"
    module_name = f"drivers.{name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as err:
        if err.name and module_name.startswith(err.name):  # le fichier du pilote n'existe pas
            raise ValueError(f"pilote inconnu « {name} » ({file} introuvable)") from err
        raise  # dépendance manquante à l'intérieur du pilote : on garde le message d'origine
    if not hasattr(module, "Driver"):
        raise ValueError(f"{file} doit exposer sa classe sous le nom « Driver »")
    return module.Driver


def load_devices(package: str = "devices") -> dict:
    """Importe chaque `devices/<id>.py`, qui doit définir `<id>` (instance de Device)."""
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


def id_from_name(name: str) -> str:
    """Identifiant déduit du nom affiché : « Prise salon » → prise_salon."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"\W+", "_", ascii_name.lower()).strip("_")


def device_label(dev_id: str) -> str:
    """« Nom (id) » d'un équipement de devices/, ou l'identifiant seul s'il est illisible."""
    try:
        name = getattr(importlib.import_module(f"devices.{dev_id}"), dev_id).name
    except Exception:  # fichier absent ou en erreur : l'identifiant suffit à le désigner
        return dev_id
    return f"{name} ({dev_id})" if name else dev_id


def device_file(dev_id: str, path: Path | None = None) -> Path:
    return (path or DEVICES_DIR) / f"{dev_id}.py"


def check_id(dev_id: str) -> None:
    """Identifiant utilisable pour devices/<id>.py ; lève ValueError sinon."""
    if not dev_id.isidentifier() or keyword.iskeyword(dev_id) or dev_id.startswith("_"):
        raise ValueError(
            f"identifiant invalide « {dev_id} » (nom de variable Python, sans « _ » initial)"
        )


def check_new_device(dev_id: str, driver: str = "", path: Path | None = None) -> None:
    """Vérifie qu'on peut déclarer cet équipement ; lève ValueError sinon."""
    check_id(dev_id)
    if device_file(dev_id, path).exists():
        raise ValueError(f"l'équipement « {dev_id} » existe déjà dans devices/")
    if driver:
        load_driver(driver)


def driver_for_model(models: str | list[str], path: Path) -> str:
    """Pilote de la table `path` ([models]) pour la première clé connue, ou "" si aucune.

    `models` : le modèle, ou plusieurs clés de la plus précise à la plus générale.
    """
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("models", {})
    except FileNotFoundError:
        return ""
    for model in [models] if isinstance(models, str) else models:
        driver = table.get(model, "")
        if not driver:
            continue
        try:
            load_driver(driver)
        except ValueError as err:
            print(f"Pilote « {driver} » associé au modèle {model} inutilisable : {err}")
            return ""
        return driver
    return ""


def add_device(
    dev_id: str,
    driver: str,
    name: str = "",
    comment: str = "",
    path: Path | None = None,
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
    file = device_file(dev_id, path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")
