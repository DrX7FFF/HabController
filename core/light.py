"""Ampoule, quel que soit le protocole : base commune des pilotes d'ampoule.

Le pilote met les attributs à jour à la réception (`Light.on.update(self, valeur)`) et fournit _switch(), _set_brightness(),
_set_color_temp() et _set_rgb() ; les méthodes publiques partent en tâche de fond."""

import logging
from abc import abstractmethod

from core.base import Capability, EventInt, EventSource, EventStr, State, Switchable

log = logging.getLogger(__name__)


def rgb_bytes(rgb: str) -> tuple[int, int, int]:
    """« #rrggbb » en (rouge, vert, bleu) ; ValueError si le format est faux."""
    text = str(rgb).lstrip("#")
    if len(text) != 6:
        raise ValueError("rgb attend une couleur au format #rrggbb")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


class Light(Switchable):
    WIDGET = "light"
    brightness = State[int]()  # 1 à 100
    color_temp = State[int]()  # Kelvin
    rgb = State[str]()  # "#rrggbb"
    rgb_capable = Capability[bool]()  # couleur prise en charge, renseigné par le pilote
    event_brightness = EventSource[EventInt]()
    event_color_temp = EventSource[EventInt]()
    event_rgb = EventSource[EventStr]()

    # --- actions : en tâche de fond, l'ampoule est allumée si besoin -------------

    def set_brightness(self, brightness: int, transition: int | None = None) -> None:
        self._background(
            f"set_brightness({brightness})", lambda: self._set_brightness(brightness, transition)
        )

    def set_color_temp(
        self, color_temp: int, brightness: int | None = None, transition: int | None = None
    ) -> None:
        self._background(
            f"set_color_temp({color_temp})",
            lambda: self._set_color_temp(color_temp, brightness, transition),
        )

    def set_rgb(self, rgb: str, brightness: int | None = None, transition: int | None = None) -> None:
        if not self.rgb_capable:
            log.warning("%s ne prend pas en charge RGB", self.id)
            return
        self._background(f"set_rgb({rgb!r})", lambda: self._set_rgb(rgb, brightness, transition))

    # --- à fournir par le pilote --------------------------------------------

    @abstractmethod
    async def _set_brightness(self, brightness: int, transition: int | None) -> None: ...

    @abstractmethod
    async def _set_color_temp(
        self, color_temp: int, brightness: int | None, transition: int | None
    ) -> None: ...

    @abstractmethod
    async def _set_rgb(self, rgb: str, brightness: int | None, transition: int | None) -> None: ...
