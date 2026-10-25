"""Emplacements des dossiers et lecture de Config/habctrl.toml."""

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


def check_config() -> None:
    """Arrête le programme avec un message clair si la configuration est introuvable."""
    if not (CONFIG_DIR / "devices").is_dir():
        sys.exit(
            f"Configuration introuvable : {CONFIG_DIR}/devices/ n'existe pas.\n"
            "Cloner le dépôt de configuration à côté de HabController (../HabConfig),"
            " ou indiquer son chemin avec HABCTRL_CONFIG."
        )


def settings(section: str) -> dict:
    """Une section de habctrl.toml ({} si absente)."""
    try:
        conf = tomllib.loads((CONFIG_DIR / "habctrl.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return conf.get(section, {})
