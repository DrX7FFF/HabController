"""Ampoule, quel que soit le protocole : base commune des pilotes d'ampoule.

Règles que chaque pilote respecte :
- on, brightness : toute ampoule ; color_temp, rgb : seulement si l'ampoule les a (sinon lecture
  None, écriture ValueError, journalisée sans rien arrêter) ;
- on accepte True, False ou "toggle" ;
- régler brightness, color_temp ou rgb allume l'ampoule éteinte.

Le pilote écrit set() ; toggle(), set_temp() et set_rgb() en découlent (deux commandes : intensité
puis réglage). Un pilote qui sait tout envoyer en un message les réécrit (ex. Yeelight).
"""

from core.base import Attribute, Device


class Light(Device):
    on = Attribute[bool]()  # True / False ; "toggle" accepté en écriture
    brightness = Attribute[int]()  # 1 à 100
    color_temp = Attribute[int]()  # Kelvin
    rgb = Attribute[str]()  # "#rrggbb"

    # Commandes groupées : en tâche de fond, comme salon.on = True.

    def toggle(self, transition: int | None = None) -> None:
        self._background("toggle", lambda: self.set("on", "toggle", transition))

    def set_temp(self, brightness: int, color_temp: int, transition: int | None = None) -> None:
        self._background(
            f"set_temp({brightness}, {color_temp})",
            lambda: self._set_temp(brightness, color_temp, transition),
        )

    def set_rgb(self, brightness: int, rgb: str, transition: int | None = None) -> None:
        self._background(
            f"set_rgb({brightness}, {rgb!r})", lambda: self._set_rgb(brightness, rgb, transition)
        )

    async def _set_temp(self, brightness: int, color_temp: int, transition: int | None) -> None:
        await self.set("brightness", brightness, transition)
        await self.set("color_temp", color_temp, transition)

    async def _set_rgb(self, brightness: int, rgb: str, transition: int | None) -> None:
        await self.set("brightness", brightness, transition)
        await self.set("rgb", rgb, transition)
