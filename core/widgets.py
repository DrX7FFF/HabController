import tomllib
from pathlib import Path

from core import CONFIG_DIR

# Widgets reconnus dans screens.toml.
WIDGETS = {"onoff", "white", "rgb"}


def load_blocks(devices: dict, path: Path = CONFIG_DIR / "screens.toml") -> list[dict]:
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
