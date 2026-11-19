"""Emplacements des dossiers et lecture de HabConfig/habctrl.toml."""

import logging
import os
import sys
import tomllib
from pathlib import Path

# calculé depuis l'emplacement du code : indépendant du dossier de lancement
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("HABCTRL_CONFIG") or ROOT.parent / "HabConfig").resolve()
PAIRING_DIR = CONFIG_DIR / "pairing"

# devices/ et automations/ sont des paquets Python de la configuration :
# `from devices.salon import salon` doit marcher d'où qu'elle soit
if str(CONFIG_DIR) not in sys.path:
    sys.path.insert(1, str(CONFIG_DIR))


def read_toml(path: Path) -> dict:
    """Contenu d'un fichier TOML : {} s'il est absent, ou illisible (erreur au journal)."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        logging.getLogger(__name__).error("%s illisible, ignoré : %s", path.name, err)
        return {}


def settings(section: str) -> dict:
    """Une section de habctrl.toml ({} si absente)."""
    section_conf = read_toml(CONFIG_DIR / "habctrl.toml").get(section, {})
    if not isinstance(section_conf, dict):
        logging.getLogger(__name__).error("habctrl.toml : [%s] n'est pas une section", section)
        return {}
    return section_conf


# chemin relatif au dossier de configuration ; par défaut temp/ du code
TEMP_DIR = (CONFIG_DIR / settings("paths").get("temp", ROOT / "temp")).resolve()
