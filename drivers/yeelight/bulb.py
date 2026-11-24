import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TypedDict, override

from yeelight import (
    BulbException,
    Flow,
    RGBTransition,
    SceneClass,
    TemperatureTransition,
)
from yeelight.aio import KEY_CONNECTED, AsyncBulb

from core.base import DeviceError, IPDevice, NetworkDevice, Retry, describe
from core.light import Light, rgb_bytes

log = logging.getLogger(__name__)

ERRORS = (BulbException, OSError)  # erreurs du protocole ou du réseau
PROPERTIES = ["power", "bright", "ct", "rgb"]


class Effect(TypedDict):
    """Transition au format de la bibliothèque."""

    effect: str
    duration: int


class YeelightBulb(Light, IPDevice, NetworkDevice):
    """Ampoule Yeelight : une connexion permanente, l'état vient des notifications."""

    BRAND = "Yeelight"

    def __init__(self, host: str, port: int = 55443, transition: int = 300):
        super().__init__(host, port)
        self.transition = transition  # durée par défaut des transitions (ms)
        self._bulb: AsyncBulb | None = None  # a besoin de la boucle asyncio : créée par start()
        Light.rgb_capable.update(self, True)

    # --- cycle de vie -------------------------------------------------------

    @override
    async def start(self) -> None:
        self._bulb = AsyncBulb(self._host, self._port, auto_on=False)
        self._spawn(self._connect(self._bulb))

    @override
    async def stop(self) -> None:
        await super().stop()
        if self._bulb:
            await self._bulb.async_stop_listening()

    async def _connect(self, bulb: AsyncBulb) -> None:
        # Après une première connexion réussie, la bibliothèque se reconnecte seule (toutes les
        # 15 s, délai fixé par elle).
        retry = Retry()
        while True:
            try:
                await bulb.async_listen(partial(self._on_update, bulb))
            except ERRORS as err:
                self._set_connected(False, reason=describe(err))
                await retry.wait()
            else:
                return

    # --- état ---------------------------------------------------------------

    def _on_update(self, bulb: AsyncBulb, props: dict[str, object]) -> None:
        """Appelé par la bibliothèque : changement d'état ou de connexion."""
        # Chaque notification porte aussi KEY_CONNECTED=True : on ne réagit qu'aux changements.
        connected = bool(props.get(KEY_CONNECTED, self._connected))
        if connected != self._connected:
            self._set_connected(connected)
            if connected:
                self._spawn(self._refresh(bulb))  # l'état a pu changer pendant la coupure
        self._merge(props)

    def _merge(self, props: dict[str, object]) -> None:
        """Valeurs reçues, noms du protocole Yeelight ("" ou absent : inchangé)."""
        if props.get("power"):
            Light.on.update(self, props["power"] == "on")
        if props.get("bright"):
            Light.brightness.update(self, int(str(props["bright"])))
        if props.get("ct"):
            Light.color_temp.update(self, int(str(props["ct"])))
        if props.get("rgb"):
            Light.rgb.update(self, f"#{int(str(props['rgb'])):06x}")

    async def _refresh(self, bulb: AsyncBulb) -> None:
        try:
            props: dict[str, object] = await bulb.async_get_properties(PROPERTIES)
        except ERRORS as err:
            log.warning("%s : lecture de l'état impossible (%s)", self.id, err)
            return
        self._merge(props)
        self._set_connected(True, synced=True)

    # --- commandes ----------------------------------------------------------
    # Avec intensité : allume et règle tout en un seul message (scène « color flow » d'une
    # étape, la seule qui accepte une transition).

    @override
    async def _switch(self, on: bool, transition: int | None) -> None:
        fx = self._effect(transition)
        await self._send(lambda bulb: bulb.async_turn_on(**fx) if on else bulb.async_turn_off(**fx))
        Light.on.update(self, on)

    @override
    async def _set_brightness(self, brightness: int, transition: int | None) -> None:
        await self._power_on(transition)
        fx = self._effect(transition)
        await self._send(lambda bulb: bulb.async_set_brightness(int(brightness), **fx))
        Light.brightness.update(self, int(brightness))

    @override
    async def _set_color_temp(
        self, color_temp: int, brightness: int | None, transition: int | None
    ) -> None:
        if brightness is None:
            await self._power_on(transition)
            fx = self._effect(transition)
            await self._send(lambda bulb: bulb.async_set_color_temp(int(color_temp), **fx))
        else:
            duration = self._duration(transition)
            step = TemperatureTransition(int(color_temp), duration, int(brightness))
            await self._scene(step)
            Light.on.update(self, True)
            Light.brightness.update(self, int(brightness))
        Light.color_temp.update(self, int(color_temp))

    @override
    async def _set_rgb(self, rgb: str, brightness: int | None, transition: int | None) -> None:
        red, green, blue = rgb_bytes(rgb)
        if brightness is None:
            await self._power_on(transition)
            fx = self._effect(transition)
            await self._send(lambda bulb: bulb.async_set_rgb(red, green, blue, **fx))
        else:
            step = RGBTransition(red, green, blue, self._duration(transition), int(brightness))
            await self._scene(step)
            Light.on.update(self, True)
            Light.brightness.update(self, int(brightness))
        Light.rgb.update(self, f"#{red:02x}{green:02x}{blue:02x}")

    async def _scene(self, step: TemperatureTransition | RGBTransition) -> None:
        flow = Flow(1, Flow.actions.stay, [step])
        await self._send(lambda bulb: bulb.async_set_scene(SceneClass.CF, flow))

    async def _send(self, command: Callable[[AsyncBulb], Awaitable[object]]) -> None:
        """Envoie une commande de la bibliothèque ; DeviceError si l'ampoule est injoignable."""
        if self._bulb is None or not self.connected:
            raise DeviceError(f"{self.id} : injoignable")
        try:
            await command(self._bulb)
        except ERRORS as err:
            raise DeviceError(f"{self.id} : {err}") from err

    def _effect(self, transition: int | None) -> Effect:
        """Transition en millisecondes (0 = immédiat)."""
        duration = self._duration(transition)
        if duration <= 0:
            return {"effect": "sudden", "duration": 0}
        return {"effect": "smooth", "duration": max(30, duration)}


Driver = YeelightBulb  # convention : le chargeur cherche ce nom
