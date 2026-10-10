import tomllib
from pathlib import Path

from core import CONFIG_DIR

# Un widget déclare les capacités dont il a besoin, quel que soit le protocole.
WIDGETS = {
    "onoff": {"needs": {"power"}},
    "white": {"needs": {"power", "brightness", "color_temp"}},
    "rgb": {"needs": {"power", "brightness", "rgb"}},
}


def load_blocks(devices: dict, path: Path = CONFIG_DIR / "screens.toml") -> list[dict]:
    conf = tomllib.loads(path.read_text(encoding="utf-8"))
    blocks = []
    for block in conf.get("blocks", []):
        dev = devices.get(block["device"])
        widget = WIDGETS.get(block["widget"])
        if dev is None:
            raise ValueError(f"écran : équipement inconnu « {block['device']} »")
        if widget is None:
            raise ValueError(f"écran : widget inconnu « {block['widget']} »")
        missing = widget["needs"] - dev.capabilities
        if missing:
            raise ValueError(
                f"écran : « {block.get('title', dev.name)} » ({block['widget']}) "
                f"demande {sorted(missing)}, absent de {dev.name}"
            )
        blocks.append(
            {"title": block.get("title", dev.name), "widget": block["widget"], "device": dev.id}
        )
    return blocks
