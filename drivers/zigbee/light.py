"""Ampoule Zigbee : marche/arrêt, intensité, température de blanc et couleur selon l'ampoule.

État : {"connected": bool, "on": bool, "brightness": 1-100, "color_temp": K, "rgb": "#rrggbb"}
Mêmes attributs que l'ampoule Yeelight (mêmes widgets) ; seuls ceux que l'ampoule gère
apparaissent (clusters 0x0008 intensité, 0x0300 couleur). La couleur passe par les
coordonnées xy de l'ampoule : le rgb relu est approché, à pleine intensité.

    from drivers.zigbee.light import Light

    plafonnier = Light(name="Plafonnier")

Écriture : plafonnier.on = True | False | "toggle", .brightness = 50, .color_temp = 2700,
.rgb = "#ff8000" ; transition en ms avec send() (300 par défaut).
"""

from core.base import Attribute, on_value
from drivers.zigbee import ZigbeeDevice, find_cluster, read, setup_reporting

ON_OFF = 0x0006
LEVEL = 0x0008
COLOR = 0x0300

# Envoi spontané : attribut -> (intervalle min s, max s, écart minimal en valeur brute)
REPORTS = {
    ON_OFF: {"on_off": (0, 600, 1)},
    LEVEL: {"current_level": (1, 600, 1)},
    COLOR: {
        "color_temperature": (1, 600, 1),
        "current_x": (1, 600, 10),
        "current_y": (1, 600, 10),
    },
}
SETTABLE = {"on": ON_OFF, "brightness": LEVEL, "color_temp": COLOR, "rgb": COLOR}


def _linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _gamma(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def rgb_to_xy(rgb: str) -> tuple[float, float]:
    """« #rrggbb » (sRGB) en coordonnées CIE xy."""
    text = str(rgb).lstrip("#")
    if len(text) != 6:
        raise ValueError("rgb attend une couleur au format #rrggbb")
    r, g, b = (_linear(int(text[i : i + 2], 16) / 255) for i in (0, 2, 4))
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    total = x + y + z
    return (x / total, y / total) if total else (0.3127, 0.3290)  # noir : point blanc


def xy_to_rgb(x: float, y: float) -> str:
    """Coordonnées CIE xy en « #rrggbb », à pleine intensité."""
    if y <= 0:
        return "#000000"
    big_x, big_z = x / y, (1 - x - y) / y
    rgb = (
        3.2406 * big_x - 1.5372 - 0.4986 * big_z,
        -0.9689 * big_x + 1.8758 + 0.0415 * big_z,
        0.0557 * big_x - 0.2040 + 1.0570 * big_z,
    )
    rgb = [max(c, 0.0) for c in rgb]
    top = max(rgb) or 1.0
    return "#" + "".join(f"{round(_gamma(c / top) * 255):02x}" for c in rgb)


class Light(ZigbeeDevice):
    """Ampoule Zigbee : l'état vient de ce qu'elle envoie, relu toutes les `poll` secondes."""

    on = Attribute[bool]()  # True / False ; "toggle" accepté en écriture
    brightness = Attribute[int]()  # 1 à 100
    color_temp = Attribute[int]()  # Kelvin
    rgb = Attribute[str]()  # "#rrggbb"

    def __init__(self, name: str = "", transition: int = 300, poll: float | None = None):
        super().__init__(name, poll)
        self.transition = transition  # durée par défaut des transitions (ms)
        self._raw: dict[str, int] = {}  # dernières valeurs ZCL reçues

    # --- état ---------------------------------------------------------------

    async def _configure(self, zdev) -> None:
        for cluster_id, reports in REPORTS.items():
            if (cluster := find_cluster(zdev, cluster_id)) is not None:
                await setup_reporting(self.name, cluster, reports)

    async def _read(self, zdev) -> None:
        for cluster_id, reports in REPORTS.items():
            if (cluster := find_cluster(zdev, cluster_id)) is None:
                continue
            for name, value in (await read(cluster, list(reports))).items():
                self._on_attribute(cluster_id, name, value)

    def _on_attribute(self, cluster_id: int, name: str, value) -> None:
        if name in REPORTS.get(cluster_id, {}) and value is not None:
            self._raw[name] = int(value)

    def _get_connected_state(self) -> dict:
        raw, state = self._raw, {}
        if "on_off" in raw:
            state["on"] = bool(raw["on_off"])
        if "current_level" in raw:
            state["brightness"] = max(1, round(raw["current_level"] * 100 / 254))
        if raw.get("color_temperature"):
            state["color_temp"] = round(1_000_000 / raw["color_temperature"])
        if "current_x" in raw and "current_y" in raw:
            state["rgb"] = xy_to_rgb(raw["current_x"] / 65535, raw["current_y"] / 65535)
        return state

    # --- commandes ----------------------------------------------------------

    async def set(self, name: str, value: bool | str | int, transition: int | None = None) -> None:
        if name not in SETTABLE:
            raise ValueError(f"{self.name} ne gère pas « {name} »")
        cluster = find_cluster(self.zdev, SETTABLE[name]) if self.zdev is not None else None
        if self.zdev is not None and cluster is None:
            raise ValueError(f"{self.name} ne gère pas « {name} » (ampoule sans ce réglage)")
        tenths = round((self.transition if transition is None else transition) / 100)
        if name == "on":
            on = on_value(value, self._raw.get("on_off") == 1)
            await self._command(cluster, "on" if on else "off")
            self._raw["on_off"] = int(on)
        elif name == "brightness":
            level = min(254, max(1, round(int(value) * 254 / 100)))
            await self._command(cluster, "move_to_level_with_on_off", level, tenths)
            self._raw.update(current_level=level, on_off=1)
        elif name == "color_temp":
            mireds = round(1_000_000 / int(value))
            await self._command(cluster, "move_to_color_temp", mireds, tenths)
            self._raw["color_temperature"] = mireds
        else:
            x, y = (round(c * 65535) for c in rgb_to_xy(str(value)))
            await self._command(cluster, "move_to_color", x, y, tenths)
            self._raw.update(current_x=x, current_y=y)
        self._notify()


Driver = Light  # convention : le chargeur cherche ce nom
