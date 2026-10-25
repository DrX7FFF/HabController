"""Pilote ampoule Zigbee."""

from core.base import DeviceError, on_value
import core.light
from drivers.zigbee import ZigbeeDevice, find_cluster, read, setup_reporting

ON_OFF = 0x0006
LEVEL = 0x0008
COLOR = 0x0300

# Réglage -> (cluster, envoi spontané : attribut -> (intervalle min s, max s, écart minimal en
# valeur brute))
SETTINGS = {
    "on": (ON_OFF, {"on_off": (0, 600, 1)}),
    "brightness": (LEVEL, {"current_level": (1, 600, 1)}),
    "color_temp": (COLOR, {"color_temperature": (1, 600, 1)}),
    "rgb": (COLOR, {"current_x": (1, 600, 10), "current_y": (1, 600, 10)}),
}
SETTING_OF = {attr: name for name, (_, reports) in SETTINGS.items() for attr in reports}
CAPABILITIES = "color_capabilities"  # attribut du cluster couleur, lu à l'appairage
CAPABILITY = {"color_temp": 0x10, "rgb": 0x08}  # bits de color_capabilities : température, xy


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


class Light(core.light.Light, ZigbeeDevice):
    """Ampoule Zigbee : l'état vient de ce qu'elle envoie, relu toutes les `poll` secondes."""

    def __init__(self, transition: int = 300, poll: float | None = None):
        super().__init__(poll)
        self.transition = transition  # durée par défaut des transitions (ms)
        self._raw: dict[str, int] = {}  # dernières valeurs ZCL reçues

    # --- état ---------------------------------------------------------------

    def _cluster(self, zdev, name: str):
        """Cluster du réglage `name`, ou None si l'ampoule ne l'a pas (cluster absent, ou
        couleur non déclarée dans color_capabilities, lu depuis le cache de la base zigpy)."""
        cluster = find_cluster(zdev, SETTINGS[name][0])
        caps = cluster.get(CAPABILITIES) if cluster is not None and name in CAPABILITY else None
        return None if caps is not None and not caps & CAPABILITY[name] else cluster

    def _reports(self, zdev) -> list:
        """[(cluster, attributs suivis)] pour les réglages que l'ampoule a."""
        tracked = {}
        for name, (cluster_id, reports) in SETTINGS.items():
            if (cluster := self._cluster(zdev, name)) is not None:
                tracked.setdefault(cluster_id, (cluster, {}))[1].update(reports)
        return list(tracked.values())

    async def _configure(self, zdev) -> None:
        for cluster, reports in self._reports(zdev):
            await setup_reporting(self.id, cluster, reports)

    async def _read(self, zdev) -> None:
        for cluster, reports in self._reports(zdev):
            for name, value in (await read(cluster, list(reports))).items():
                self._on_attribute(cluster.cluster_id, name, value)

    def _on_attribute(self, cluster_id: int, name: str, value) -> None:
        if name not in SETTING_OF or value is None:
            return
        # ex. une ampoule blanc réglable envoie aussi x/y : pas de rgb pour elle
        if self.zdev is None or self._cluster(self.zdev, SETTING_OF[name]) is not None:
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
        if name not in SETTINGS:
            raise ValueError(f"{self.id} ne gère pas « {name} »")
        if self.zdev is None:
            raise DeviceError(f"{self.id} : injoignable")
        if (cluster := self._cluster(self.zdev, name)) is None:
            raise ValueError(f"{self.id} ne gère pas « {name} » (ampoule sans ce réglage)")
        tenths = round((self.transition if transition is None else transition) / 100)
        if name in CAPABILITY and self._raw.get("on_off") != 1:
            # une ampoule éteinte ignore les commandes de couleur : on l'allume d'abord
            await self._command(find_cluster(self.zdev, ON_OFF), "on")
            self._raw["on_off"] = 1
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
