"""Ampoule, quel que soit le protocole : base commune des pilotes d'ampoule.

Le pilote met les attributs à jour à la réception (`Light.on.update(self, valeur)`) et fournit _switch(), _set_brightness(),
_set_color_temp() et _set_rgb() ; les méthodes publiques partent en tâche de fond."""

import logging

from core.base import EventInt, EventSource, EventStr, State, Switchable, known

log = logging.getLogger(__name__)


class Light(Switchable):
    WIDGET = "light"
    brightness = State[int]()  # 1 à 100
    color_temp = State[int]()  # Kelvin
    rgb = State[str]()  # "#rrggbb"
    rgb_capable = State[bool]()  # couleur prise en charge, renseigné par le pilote
    event_brightness = EventSource[EventInt]()
    event_color_temp = EventSource[EventInt]()
    event_rgb = EventSource[EventStr]()

    def state(self) -> dict:
        state = super().state()
        if not state.get("connected"):
            return state
        return state | known(brightness=self.brightness, color_temp=self.color_temp, rgb=self.rgb)

    def measures(self) -> dict[str, str]:
        tracked = {"brightness": "tracked", "color_temp": "tracked", "rgb": "tracked"}
        return tracked | super().measures()

    def _reset(self) -> None:
        for state in (Light.brightness, Light.color_temp, Light.rgb):
            state.update(self, None)
        super()._reset()

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

    async def _set_brightness(self, brightness: int, transition: int | None) -> None:
        raise NotImplementedError

    async def _set_color_temp(
        self, color_temp: int, brightness: int | None, transition: int | None
    ) -> None:
        raise NotImplementedError

    async def _set_rgb(self, rgb: str, brightness: int | None, transition: int | None) -> None:
        raise NotImplementedError
