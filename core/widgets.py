import logging
import tomllib
from pathlib import Path

from core import CONFIG_DIR

log = logging.getLogger(__name__)

# Widgets reconnus dans dashboard/screens.toml : commandes (onoff, white, rgb) ou mesures
# d'un capteur, en lecture seule (sensor).
WIDGETS = {"onoff", "white", "rgb", "sensor"}


SCREENS = CONFIG_DIR / "dashboard" / "screens.toml"


def load_blocks(devices: dict, path: Path = SCREENS) -> list[dict]:
    """Blocs de l'écran ; bloc d'équipement ou de widget inconnu : ignoré, erreur au journal."""
    conf = tomllib.loads(path.read_text(encoding="utf-8"))
    blocks = []
    for block in conf.get("blocks", []):
        dev = devices.get(block["device"])
        if dev is None:
            log.error("écran : équipement inconnu « %s », bloc ignoré", block["device"])
            continue
        if block["widget"] not in WIDGETS:
            log.error("écran : widget inconnu « %s », bloc ignoré", block["widget"])
            continue
        blocks.append(
            {"title": block.get("title", dev.id), "widget": block["widget"], "device": dev.id}
        )
    return blocks
