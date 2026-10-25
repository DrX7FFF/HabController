"""Emplacements des dossiers et lecture de Config/habctrl.toml."""

import logging
import os
import sys
import tomllib
from pathlib import Path

# calculé depuis l'emplacement du code : indépendant du dossier de lancement
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("HABCTRL_CONFIG") or ROOT.parent / "HabConfig").resolve()
TEMP_DIR = Path(os.environ.get("HABCTRL_TEMP") or ROOT / "temp").resolve()
PAIRING_DIR = CONFIG_DIR / "pairing"

# devices/ et automations/ sont des paquets Python de la configuration :
# `from devices.salon import salon` doit marcher d'où qu'elle soit
if str(CONFIG_DIR) not in sys.path:
    sys.path.insert(1, str(CONFIG_DIR))


def settings(section: str) -> dict:
    """Une section de habctrl.toml ({} si absente, ou fichier illisible : erreur au journal)."""
    try:
        conf = tomllib.loads((CONFIG_DIR / "habctrl.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        logging.getLogger(__name__).error("habctrl.toml illisible, [%s] ignoré : %s", section, err)
        return {}
    section_conf = conf.get(section, {})
    if not isinstance(section_conf, dict):
        logging.getLogger(__name__).error("habctrl.toml : [%s] n'est pas une section", section)
        return {}
    return section_conf
