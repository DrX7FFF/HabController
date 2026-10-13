"""Emplacements : l'application (ce dépôt), la configuration et les fichiers temporaires.

- config/ : tout ce qui décrit l'installation et qu'il faut garder pour la restaurer —
  réglages (habctrl.toml), tableau de bord (dashboard/), équipements (devices/),
  automatisations (automations/) et appairages (pairing/). Destinée à un dépôt privé.
- temp/ : 100 % jetable, peut être en RAM — journaux, base Zigbee vivante (recréée au
  démarrage depuis l'instantané de config/pairing/).

Par défaut dans le dossier du projet ; HABCTRL_CONFIG et HABCTRL_TEMP les déplacent.
"""

import os
import sys
import tomllib
from pathlib import Path

# calculé depuis l'emplacement du code : indépendant du dossier de lancement
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("HABCTRL_CONFIG") or ROOT / "config").resolve()
TEMP_DIR = Path(os.environ.get("HABCTRL_TEMP") or ROOT / "temp").resolve()
PAIRING_DIR = CONFIG_DIR / "pairing"

# devices/ et automations/ sont des paquets Python de la configuration :
# `from devices.salon import salon` doit marcher d'où qu'elle soit
if str(CONFIG_DIR) not in sys.path:
    sys.path.insert(1, str(CONFIG_DIR))


def settings(section: str) -> dict:
    """Une section de config/habctrl.toml ({} si absente)."""
    try:
        conf = tomllib.loads((CONFIG_DIR / "habctrl.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return conf.get(section, {})
