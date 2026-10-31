import asyncio
import importlib
import keyword
import logging
import pkgutil
import re
import time
import tomllib
import unicodedata
from pathlib import Path
from types import ModuleType

import drivers

from core import CONFIG_DIR
from core.base import Device

log = logging.getLogger(__name__)

DEVICES_DIR = CONFIG_DIR / "devices"
NEW_DEVICES = "devices.py"  # où l'appairage ajoute les équipements, à réorganiser librement
DRIVERS_IMPORT = "from drivers.all import "  # import commun des classes de pilotes


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
    le nom de la variable. Un même équipement importé dans un autre fichier compte une fois ; un
    identifiant déclaré dans deux fichiers est signalé (le dernier chargé remplace l'autre).
    Un fichier en erreur est ignoré (erreur au journal), les autres sont chargés."""
    devices = {}
    files = {}  # id -> fichier qui le déclare
    try:
        modules = list(pkgutil.iter_modules(importlib.import_module(package).__path__))
    except Exception:
        log.exception("%s/ introuvable ou illisible : aucun équipement", package)
        return devices
    for info in sorted(modules, key=lambda module: module.name):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{package}.{info.name}")
        except Exception:
            log.exception("%s/%s.py : fichier ignoré", package, info.name)
            continue
        for dev_id, device in vars(module).items():
            if isinstance(device, Device) and device not in devices.values():
                if dev_id in devices:
                    log.warning(
                        "%s : déclaré deux fois (%s.py, %s.py)", dev_id, files[dev_id], info.name
                    )
                device.configure(dev_id)
                devices[dev_id] = device
                files[dev_id] = info.name
    return devices


async def wait_ready() -> None:
    """Rend la main quand chaque protocole qui fournit ready() connaît ses appareils."""
    await asyncio.gather(
        *(module.ready() for module in protocol_modules() if hasattr(module, "ready"))
    )


async def summarize(devices: dict[str, Device], path: Path) -> None:
    """Écrit le résumé (voir write_summary) une fois les appareils connus (voir wait_ready).
    Anomalies (non appairé, non déclaré) : un avertissement dans le journal."""
    await wait_ready()
    if anomalies := write_summary(devices, path):
        log.warning("Équipements : %d anomalie(s) d'appairage, voir %s", anomalies, path)


def protocol_modules() -> list[ModuleType]:
    """Paquets de drivers/ (un par protocole ou marque)."""
    return [
        importlib.import_module(f"drivers.{info.name}")
        for info in pkgutil.iter_modules(drivers.__path__)
    ]


def write_summary(devices: dict[str, Device], path: Path) -> int:
    """Rapport YAML : d'abord les appareils appairés sans équipement déclaré, chacun avec la
    déclaration à copier dans devices/ si le modèle est connu, puis la fiche de chaque équipement
    (pilote et appareil physique, voir Device.details ; « not paired » sinon). Un protocole
    fournit les premiers par links() et link_models() (lien enregistré sous un id), ou par
    undeclared() (lien porté par la déclaration : id à choisir ; None s'il ne les connaît pas :
    rapport non écrit). Renvoie le nombre d'anomalies : non appairés et non déclarés."""
    unlinked: dict = {}  # appareil : sa déclaration
    orphans = 0
    for module in protocol_modules():
        models_path = Path(module.__file__).with_name("models.toml")
        if links := getattr(module, "links", None):
            found = [(i, p) for i, p in sorted(links().items()) if i not in devices]
            link_models = getattr(module, "link_models", None)
            known = link_models() if found and link_models else {}
            for dev_id, physical in found:
                keys, comment = known.get(dev_id, ([], ""))
                unlinked[f"{dev_id}  {physical}"] = orphan(dev_id, keys, comment, (), models_path)
                orphans += 1
        if undeclared := getattr(module, "undeclared", None):
            if (found := undeclared()) is None:
                log.info("Équipements : rapport non écrit, %s non initialisé", module.__name__)
                return 0
            for physical, keys, comment, args in found:
                unlinked[physical] = orphan("<id>", keys, comment, args, models_path)
                orphans += 1
    report = {"unlinked": unlinked or "none"}
    unpaired = 0
    for dev_id, device in sorted(devices.items()):
        info = {"driver": type(device).__module__.removeprefix("drivers.")}
        if device.physical():
            info |= device.details()
        else:
            unpaired += 1
            info["physical"] = "not paired"
        report[dev_id] = info
    lines = [
        f"# Devices at {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "# unlinked: physical devices without equipment in devices/",
        "",
    ]
    for key, value in report.items():
        lines += yaml({key: value}, 0) + [""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return unpaired + orphans


def orphan(
    dev_id: str, keys: list[str], comment: str, args: tuple[str, ...], models: Path
) -> dict:
    """Déclaration à copier pour un appareil sans équipement, si le pilote est connu (table
    `models`)."""
    try:
        statement, call = constructor(driver_for_model(keys, models), args)
    except ValueError:
        return {"declaration": "unknown driver, to declare by hand"}
    line = f"{dev_id} = {call}" + (f"  # {comment}" if comment else "")
    return {"declaration": f"{statement}\n{line}"}


def yaml(data: dict, indent: int) -> list[str]:
    """Fiche en YAML lisible : libellés alignés, fiches imbriquées indentées, texte sur
    plusieurs lignes en bloc (« | »)."""
    pad = "  " * indent
    width = max(len(str(key)) for key in data)
    lines = []
    for key, value in data.items():
        if isinstance(value, dict):
            lines += [f"{pad}{key}:", *yaml(value, indent + 1)]
        elif "\n" in str(value):
            lines += [f"{pad}{key}: |", *(f"{pad}  {line}" for line in str(value).splitlines())]
        else:
            lines.append(f"{pad}{str(key).ljust(width)} : {value}")
    return lines


def id_from_name(name: str) -> str:
    """Identifiant déduit d'un texte libre : « Prise salon » → prise_salon."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"\W+", "_", ascii_name.lower()).strip("_")


def check_id(dev_id: str) -> None:
    """Identifiant utilisable pour un équipement (nom de variable) ; lève ValueError sinon."""
    if not dev_id.isidentifier() or keyword.iskeyword(dev_id) or dev_id.startswith("_"):
        raise ValueError(
            f"identifiant invalide « {dev_id} » (nom de variable Python, sans « _ » initial)"
        )


def driver_for_model(models: list[str], path: Path) -> str:
    """Pilote de la table `path` ([models]) pour la première clé connue, ou "" si aucune.

    `models` : clés de la plus précise à la plus générale.
    """
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("models", {})
    except FileNotFoundError:
        return ""
    for model in models:
        driver = table.get(model, "")
        if not driver:
            continue
        try:
            load_driver(driver)
        except ValueError as err:
            log.warning("Pilote « %s » associé au modèle %s inutilisable : %s", driver, model, err)
            return ""
        return driver
    return ""


def constructor(driver: str, args: tuple[str, ...] = ()) -> tuple[str, str]:
    """(import du pilote, `<Pilote>(...)`), avec les arguments `args` (ex. 'ieee="…"') puis les
    réglages par défaut du pilote."""
    cls = load_driver(driver)
    config_args = getattr(cls, "config_args", None)
    args += tuple(config_args()) if config_args else ()
    return f"{DRIVERS_IMPORT}{cls.__name__}", f"{cls.__name__}({', '.join(args)})"


def declaration(dev_id: str, driver: str, args: tuple[str, ...] = ()) -> tuple[str, str]:
    """(import du pilote, `<id> = <Pilote>(...)`), voir constructor."""
    check_id(dev_id)
    statement, call = constructor(driver, args)
    return statement, f"{dev_id} = {call}"


def add_device(
    dev_id: str,
    driver: str,
    comment: str = "",
    args: tuple[str, ...] = (),
    path: Path | None = None,
) -> Path:
    """Ajoute la déclaration de l'équipement (arguments `args`, voir declaration) à la fin de
    devices/devices.py (créé au besoin), avec sa classe dans l'import `from drivers.all import ...`
    (complété, ou ajouté). Renvoie le fichier."""
    statement, declared = declaration(dev_id, driver, args)
    file = (path or DEVICES_DIR) / NEW_DEVICES
    lines = file.read_text(encoding="utf-8").splitlines() if file.exists() else []
    common = [i for i, line in enumerate(lines) if line.startswith(DRIVERS_IMPORT)]
    if common:
        names = {n.strip() for n in lines[common[0]].removeprefix(DRIVERS_IMPORT).split(",")}
        if "*" not in names:
            names.add(statement.removeprefix(DRIVERS_IMPORT))
            lines[common[0]] = DRIVERS_IMPORT + ", ".join(sorted(names))
    else:
        imports = [i for i, line in enumerate(lines) if line.startswith(("from ", "import "))]
        lines.insert(imports[-1] + 1 if imports else 0, statement)
    lines += [""] + ([f"# {comment}"] if comment else []) + [declared]
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return file
