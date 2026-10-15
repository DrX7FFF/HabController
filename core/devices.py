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
NEW_DEVICES = "devices.py"  # où l'appairage ajoute les équipements, à réorganiser librement


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
    """Importe chaque `devices/*.py` : toute variable Device est un équipement, d'identifiant
    le nom de la variable. Un même équipement importé dans un autre fichier compte une fois."""
    devices = {}
    modules = pkgutil.iter_modules(importlib.import_module(package).__path__)
    for info in sorted(modules, key=lambda module: module.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package}.{info.name}")
        for dev_id, device in vars(module).items():
            if isinstance(device, Device) and device not in devices.values():
                device.configure(dev_id)
                devices[dev_id] = device
    return devices


def id_from_name(name: str) -> str:
    """Identifiant déduit du nom affiché : « Prise salon » → prise_salon."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"\W+", "_", ascii_name.lower()).strip("_")


def check_id(dev_id: str) -> None:
    """Identifiant utilisable pour un équipement (nom de variable) ; lève ValueError sinon."""
    if not dev_id.isidentifier() or keyword.iskeyword(dev_id) or dev_id.startswith("_"):
        raise ValueError(
            f"identifiant invalide « {dev_id} » (nom de variable Python, sans « _ » initial)"
        )


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
) -> Path:
    """Ajoute `<id> = <Pilote>(...)` à la fin de devices/devices.py (créé au besoin), avec les
    réglages par défaut du pilote et son import s'il manque. Renvoie le fichier."""
    check_id(dev_id)
    if not driver:
        raise ValueError("pilote requis")
    cls = load_driver(driver)
    args = [f"name={json.dumps(name, ensure_ascii=False)}"] if name else []
    config_args = getattr(cls, "config_args", None)
    if config_args:
        args += config_args()
    file = (path or DEVICES_DIR) / NEW_DEVICES
    lines = file.read_text(encoding="utf-8").splitlines() if file.exists() else []
    statement = f"from drivers.{driver} import {cls.__name__}"
    if statement not in lines:
        imports = [i for i, line in enumerate(lines) if line.startswith(("from ", "import "))]
        lines.insert(imports[-1] + 1 if imports else 0, statement)
    lines += [""] + ([f"# {comment}"] if comment else [])
    lines.append(f"{dev_id} = {cls.__name__}({', '.join(args)})")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return file
