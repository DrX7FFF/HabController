"""Pilote ampoule Zigbee."""

from typing import TypedDict, override

import core.light
from core.base import DeviceError
from drivers.zigbee import ON_OFF, Measure, ZigbeeDevice

LEVEL = 0x0008
COLOR = 0x0300

# Réglage -> mesure (envoi spontané : intervalle min s, max s, écart minimal en valeur brute) ;
# écart de brightness : celui de reports, converti ; rgb : converti par le pilote (x et y)
MEASURES = {
    "on": Measure(ON_OFF, ("on_off",), 0, 600, lambda _: 1, bool),
    "brightness": Measure(
        LEVEL, ("current_level",), 1, 600, lambda percent: percent * 254 / 100,
        lambda v: max(1, round(int(v) * 100 / 254)),
    ),
    "color_temp": Measure(
        COLOR, ("color_temperature",), 1, 600, lambda _: 1,
        lambda v: round(1_000_000 / int(v)) if v else None,
    ),
    "rgb": Measure(COLOR, ("current_x", "current_y"), 1, 600, lambda _: 10),
}
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
    r, g, b = (_linear(c / 255) for c in core.light.rgb_bytes(rgb))
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
    MEASURES = MEASURES

    def __init__(
        self,
        ieee: str,
        transition: int = 300,
        poll: float | None = None,
        reports: LightReports | None = None,
    ):
        super().__init__(ieee, poll, reports)
        self.transition = transition  # durée par défaut des transitions (ms)
        self._xy: dict[str, int] = {}  # dernières coordonnées de couleur reçues (current_x/y)

    # --- état ---------------------------------------------------------------

    @override
    def _cluster(self, zdev, key: str):
        """Cluster du réglage `key`, ou None si l'ampoule ne l'a pas (cluster absent, ou
        couleur non déclarée dans color_capabilities, lu depuis le cache de la base zigpy)."""
        cluster = super()._cluster(zdev, key)
        caps = cluster.get(CAPABILITIES) if cluster is not None and key in CAPABILITY else None
        return None if caps is not None and not caps & CAPABILITY[key] else cluster

    @override
    def _attach(self, app) -> None:
        super()._attach(app)
        if self.zdev is not None:
            Light.rgb_capable.update(self, self._cluster(self.zdev, "rgb") is not None)

    @override
    def _on_attribute(self, cluster_id: int, name: str, value) -> None:
        if name in MEASURES["rgb"].attrs and value is not None:
            self._xy[name] = int(value)
        super()._on_attribute(cluster_id, name, value)

    @override
    def _value(self, key: str, value):
        # ex. une ampoule blanc réglable envoie aussi x/y : pas de rgb pour elle
        if self.zdev is not None and self._cluster(self.zdev, key) is None:
            return None
        if key != "rgb":
            return super()._value(key, value)
        if len(self._xy) < 2:
            return None
        return xy_to_rgb(self._xy["current_x"] / 65535, self._xy["current_y"] / 65535)

    # --- commandes ----------------------------------------------------------

    def _tenths(self, transition: int | None) -> int:
        return round(self._duration(transition) / 100)

    def _setting(self, name: str):
        """Cluster du réglage `name` ; DeviceError si injoignable, ValueError si l'ampoule ne
        l'a pas."""
        if self.zdev is None:
            raise DeviceError(f"{self.id} : injoignable")
        if (cluster := self._cluster(self.zdev, name)) is None:
            raise ValueError(f"{self.id} ne gère pas « {name} » (ampoule sans ce réglage)")
        return cluster

    @override
    async def _switch(self, on: bool, transition: int | None) -> None:
        await self._command(self._setting("on"), "on" if on else "off")
        Light.on.update(self, on)

    @override
    async def _set_brightness(self, brightness: int, transition: int | None) -> None:
        level = min(254, max(1, round(int(brightness) * 254 / 100)))
        await self._command(
            self._setting("brightness"), "move_to_level_with_on_off", level,
            self._tenths(transition),
        )
        Light.on.update(self, True)
        Light.brightness.update(self, int(brightness))

    @override
    async def _set_color_temp(
        self, color_temp: int, brightness: int | None, transition: int | None
    ) -> None:
        if brightness is not None:
            await self._set_brightness(brightness, transition)
        cluster = await self._color_cluster("color_temp")
        mireds = round(1_000_000 / int(color_temp))
        await self._command(cluster, "move_to_color_temp", mireds, self._tenths(transition))
        Light.color_temp.update(self, int(color_temp))

    @override
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
        await self._power_on(None)
        return cluster


Driver = Light  # convention : le chargeur cherche ce nom
