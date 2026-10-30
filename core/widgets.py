import logging
import tomllib
from pathlib import Path

from core import CONFIG_DIR

log = logging.getLogger(__name__)

SCREENS = CONFIG_DIR / "dashboard" / "screens.toml"


def load_blocks(devices: dict, path: Path = SCREENS) -> list[dict]:
    """Blocs de l'écran, chacun avec le widget de son équipement (voir Device.WIDGET) ; bloc
    incorrect (équipement inconnu ou sans widget…) : ignoré, erreur au journal. Fichier absent :
    aucun bloc ; illisible : aucun bloc, erreur au journal."""
    try:
        conf = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        log.error("%s illisible, aucun bloc : %s", path.name, err)
        return []
    blocks = []
    for block in conf.get("blocks", []):
        if not isinstance(block, dict) or "device" not in block:
            log.error("écran : bloc sans device, ignoré : %s", block)
            continue
        dev = devices.get(block["device"])
        if dev is None:
            log.error("écran : équipement inconnu « %s », bloc ignoré", block["device"])
            continue
        if not dev.WIDGET:
            log.error("écran : « %s » n'a pas de widget, bloc ignoré", dev.id)
            continue
        blocks.append(
            {"title": str(block.get("title", dev.id)), "widget": dev.WIDGET, "device": dev.id}
        )
    return blocks
