import tomllib
from pathlib import Path

from core import CONFIG_DIR

# Widgets reconnus dans dashboard/screens.toml : commandes (onoff, white, rgb) ou mesures
# d'un capteur, en lecture seule (sensor).
WIDGETS = {"onoff", "white", "rgb", "sensor"}


SCREENS = CONFIG_DIR / "dashboard" / "screens.toml"


def load_blocks(devices: dict, path: Path = SCREENS) -> list[dict]:
    conf = tomllib.loads(path.read_text(encoding="utf-8"))
    blocks = []
    for block in conf.get("blocks", []):
        dev = devices.get(block["device"])
        if dev is None:
            raise ValueError(f"écran : équipement inconnu « {block['device']} »")
        if block["widget"] not in WIDGETS:
            raise ValueError(f"écran : widget inconnu « {block['widget']} »")
        blocks.append(
            {"title": block.get("title", dev.name), "widget": block["widget"], "device": dev.id}
        )
    return blocks
