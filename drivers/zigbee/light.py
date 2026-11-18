"""Pilote ampoule Zigbee."""

from typing import TypedDict

from core.base import DeviceError
import core.light
from drivers.zigbee import (
    ZigbeeDevice,
    find_cluster,
    raw_change,
    read,
    setup_reporting,
    tracked,
)

ON_OFF = 0x0006
LEVEL = 0x0008
COLOR = 0x0300

# Réglage -> (cluster, envoi spontané : attribut -> (intervalle min s, max s, écart minimal en
# valeur brute)) ; écart de brightness : celui de reports, converti
SETTINGS = {
    "on": (ON_OFF, {"on_off": (0, 600, 1)}),
    "brightness": (LEVEL, {"current_level": (1, 600, 1)}),
    "color_temp": (COLOR, {"color_temperature": (1, 600, 1)}),
    "rgb": (COLOR, {"current_x": (1, 600, 10), "current_y": (1, 600, 10)}),
}
SETTING_OF = {attr: name for name, (_, reports) in SETTINGS.items() for attr in reports}
CAPABILITIES = "color_capabilities"  # attribut du cluster couleur, lu à l'appairage
CAPABILITY = {"color_temp": 0x10, "rgb": 0x08}  # bits de color_capabilities : température, xy


class LightReports(TypedDict, total=False):
    """Réglages suivis : True, ou écart minimal entre deux envois ; None : non suivi."""

    on: bool | None
    brightness: float | None  # %
    color_temp: bool | None
    rgb: bool | None


DEFAULTS: LightReports = {"on": True, "brightness": 1, "color_temp": True, "rgb": True}


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

    DEFAULTS = DEFAULTS

    def __init__(
        self,
        ieee: str,
        transition: int = 300,
        poll: float | None = None,
        reports: LightReports | None = None,
    ):
        super().__init__(ieee, poll)
        self.reports = tracked(DEFAULTS, reports)
        self.transition = transition  # durée par défaut des transitions (ms)
        self._xy: dict[str, int] = {}  # dernières coordonnées de couleur reçues (current_x/y)

    # --- état ---------------------------------------------------------------

    def _cluster(self, zdev, name: str):
        """Cluster du réglage `name`, ou None si l'ampoule ne l'a pas (cluster absent, ou
        couleur non déclarée dans color_capabilities, lu depuis le cache de la base zigpy)."""
        cluster = find_cluster(zdev, SETTINGS[name][0])
        caps = cluster.get(CAPABILITIES) if cluster is not None and name in CAPABILITY else None
        return None if caps is not None and not caps & CAPABILITY[name] else cluster

    def _reports(self, zdev) -> list:
        """[(cluster, envoi spontané par attribut)] pour les réglages que l'ampoule a ; None :
        réglage non suivi, l'ampoule doit cesser de l'envoyer (elle garde sa configuration)."""
        clusters = {}
        for name, (cluster_id, reports) in SETTINGS.items():
            if (cluster := self._cluster(zdev, name)) is None:
                continue
            cfgs = clusters.setdefault(cluster_id, (cluster, {}))[1]
            for attr, (low, high, change) in reports.items():
                if name not in self.reports:
                    cfgs[attr] = None
                elif name == "brightness":
                    cfgs[attr] = (low, high, raw_change(self.reports[name] * 254 / 100))
                else:
                    cfgs[attr] = (low, high, change)
        return list(clusters.values())

    async def _configure(self, zdev) -> None:
        for cluster, reports in self._reports(zdev):
            await setup_reporting(self.id, cluster, reports)

    async def _read(self, zdev) -> None:
        for cluster, reports in self._reports(zdev):
            names = [attr for attr, cfg in reports.items() if cfg is not None]
            if not names:
                continue
            for name, value in (await read(cluster, names)).items():
                self._on_attribute(cluster.cluster_id, name, value)

    def _attach(self, app) -> None:
        super()._attach(app)
        if self.zdev is not None:
            Light.rgb_capable.update(self, self._cluster(self.zdev, "rgb") is not None)

    def _on_attribute(self, cluster_id: int, name: str, value) -> None:
        setting = SETTING_OF.get(name)
        if setting not in self.reports or value is None:
            return
        # ex. une ampoule blanc réglable envoie aussi x/y : pas de rgb pour elle
        if self.zdev is not None and self._cluster(self.zdev, setting) is None:
            return
        if name == "on_off":
            Light.on.update(self, bool(value))
        elif name == "current_level":
            Light.brightness.update(self, max(1, round(int(value) * 100 / 254)))
        elif name == "color_temperature":
            if value:
                Light.color_temp.update(self, round(1_000_000 / int(value)))
        else:
            self._xy[name] = int(value)
            if len(self._xy) == 2:
                x, y = self._xy["current_x"] / 65535, self._xy["current_y"] / 65535
                Light.rgb.update(self, xy_to_rgb(x, y))

    # --- commandes ----------------------------------------------------------

    def _tenths(self, transition: int | None) -> int:
        return round((self.transition if transition is None else transition) / 100)

    def _setting(self, name: str):
        """Cluster du réglage `name` ; DeviceError si injoignable, ValueError si l'ampoule ne
        l'a pas."""
        if self.zdev is None:
            raise DeviceError(f"{self.id} : injoignable")
        if (cluster := self._cluster(self.zdev, name)) is None:
            raise ValueError(f"{self.id} ne gère pas « {name} » (ampoule sans ce réglage)")
        return cluster

    async def _switch(self, on: bool, transition: int | None) -> None:
        await self._command(self._setting("on"), "on" if on else "off")
        Light.on.update(self, on)

    async def _set_brightness(self, brightness: int, transition: int | None) -> None:
        level = min(254, max(1, round(int(brightness) * 254 / 100)))
        await self._command(
            self._setting("brightness"), "move_to_level_with_on_off", level, self._tenths(transition)
        )
        Light.on.update(self, True)
        Light.brightness.update(self, int(brightness))

    async def _set_color_temp(
        self, color_temp: int, brightness: int | None, transition: int | None
    ) -> None:
        if brightness is not None:
            await self._set_brightness(brightness, transition)
        cluster = await self._color_cluster("color_temp")
        mireds = round(1_000_000 / int(color_temp))
        await self._command(cluster, "move_to_color_temp", mireds, self._tenths(transition))
        Light.color_temp.update(self, int(color_temp))

    async def _set_rgb(self, rgb: str, brightness: int | None, transition: int | None) -> None:
        if brightness is not None:
            await self._set_brightness(brightness, transition)
        cluster = await self._color_cluster("rgb")
        x, y = (round(c * 65535) for c in rgb_to_xy(rgb))
        await self._command(cluster, "move_to_color", x, y, self._tenths(transition))
        Light.rgb.update(self, rgb)

    async def _color_cluster(self, name: str):
        """Cluster couleur ; une ampoule éteinte ignore les commandes de couleur : on l'allume
        d'abord."""
        cluster = self._setting(name)
        if not self.on:
            await self._switch(True, None)
        return cluster


Driver = Light  # convention : le chargeur cherche ce nom
