import asyncio
import logging

from yeelight import BulbException, Flow, RGBTransition, SceneClass, TemperatureTransition
from yeelight.aio import KEY_CONNECTED, AsyncBulb

from core.base import DeviceError, IPDevice, NetworkDevice
from core.light import Light, rgb_bytes

log = logging.getLogger(__name__)

ERRORS = (BulbException, OSError)  # erreurs du protocole ou du réseau
PROPERTIES = ["power", "bright", "ct", "rgb"]
RETRY_DELAY = 10  # s, entre deux tentatives de première connexion


class YeelightBulb(Light, IPDevice, NetworkDevice):
    """Ampoule Yeelight : une connexion permanente, l'état vient des notifications."""

    BRAND = "Yeelight"

    def __init__(
        self,
        host: str,
        port: int = 55443,
        transition: int = 300,
    ):
        super().__init__(host, port)
        self.transition = transition  # durée par défaut des transitions (ms)
        self._bulb: AsyncBulb | None = None
        Light.rgb_capable.update(self, True)

    # --- cycle de vie -------------------------------------------------------


    async def start(self) -> None:
        # AsyncBulb a besoin de la boucle d'événements en cours : création ici, pas dans __init__.
        self._bulb = AsyncBulb(self._host, self._port, auto_on=False)
        self._spawn(self._connect())

    async def stop(self) -> None:
        await super().stop()
        if self._bulb:
            await self._bulb.async_stop_listening()

    async def _connect(self) -> None:
        # Après une première connexion réussie, la bibliothèque se reconnecte seule.
        while True:
            try:
                await self._bulb.async_listen(self._on_update)
            except ERRORS as err:
                log.warning("%s : connexion impossible (%s), nouvel essai", self.id, err)
                await asyncio.sleep(RETRY_DELAY)
            else:
                return

    # --- état ---------------------------------------------------------------

    def _on_update(self, props: dict) -> None:
        """Appelé par la bibliothèque : changement d'état ou de connexion."""
        # Chaque notification porte aussi KEY_CONNECTED=True : on ne réagit qu'aux changements.
        connected = props.get(KEY_CONNECTED, self._connected)
        if connected != self._connected:
            self._set_connected(connected)
            if connected:
                self._spawn(self._refresh())  # l'état a pu changer pendant la coupure
        self._merge(props)

    def _merge(self, props: dict) -> None:
        """Valeurs reçues, noms du protocole Yeelight ("" ou absent : inchangé)."""
        if props.get("power"):
            Light.on.update(self, props["power"] == "on")
        if props.get("bright"):
            Light.brightness.update(self, int(props["bright"]))
        if props.get("ct"):
            Light.color_temp.update(self, int(props["ct"]))
        if props.get("rgb"):
            Light.rgb.update(self, f"#{int(props['rgb']):06x}")

    async def _refresh(self) -> None:
        try:
            props = await self._bulb.async_get_properties(PROPERTIES)
        except ERRORS as err:
            log.warning("%s : lecture de l'état impossible (%s)", self.id, err)
            return
        self._merge(props)
        self._set_connected(True, synced=True)

    # --- commandes ----------------------------------------------------------
    # Avec intensité : allume et règle tout en un seul message (scène « color flow » d'une
    # étape, la seule qui accepte une transition).

    async def _switch(self, on: bool, transition: int | None) -> None:
        fx = self._effect(transition)
        await self._send(self._bulb.async_turn_on(**fx) if on else self._bulb.async_turn_off(**fx))
        Light.on.update(self, on)

    async def _set_brightness(self, brightness: int, transition: int | None) -> None:
        await self._power_on(transition)
        await self._send(self._bulb.async_set_brightness(int(brightness), **self._effect(transition)))
        Light.brightness.update(self, int(brightness))

    async def _set_color_temp(
        self, color_temp: int, brightness: int | None, transition: int | None
    ) -> None:
        if brightness is None:
            await self._power_on(transition)
            await self._send(
                self._bulb.async_set_color_temp(int(color_temp), **self._effect(transition))
            )
        else:
            step = TemperatureTransition(
                int(color_temp), self._duration(transition), int(brightness)
            )
            await self._scene(step)
            Light.on.update(self, True)
            Light.brightness.update(self, int(brightness))
        Light.color_temp.update(self, int(color_temp))

    async def _set_rgb(self, rgb: str, brightness: int | None, transition: int | None) -> None:
        red, green, blue = rgb_bytes(rgb)
        if brightness is None:
            await self._power_on(transition)
            await self._send(self._bulb.async_set_rgb(red, green, blue, **self._effect(transition)))
        else:
            step = RGBTransition(red, green, blue, self._duration(transition), int(brightness))
            await self._scene(step)
            Light.on.update(self, True)
            Light.brightness.update(self, int(brightness))
        Light.rgb.update(self, f"#{red:02x}{green:02x}{blue:02x}")

    async def _power_on(self, transition: int | None) -> None:
        """L'ampoule refuse les réglages quand elle est éteinte : on l'allume d'abord."""
        if not self.on:
            await self._switch(True, transition)

    async def _scene(self, step) -> None:
        await self._send(self._bulb.async_set_scene(SceneClass.CF, Flow(1, Flow.actions.stay, [step])))

    async def _send(self, command) -> None:
        """Envoie une commande de la bibliothèque ; DeviceError si l'ampoule est injoignable."""
        try:
            self._check_reachable()
            await command
        except ERRORS as err:
            raise DeviceError(f"{self.id} : {err}") from err
        finally:
            command.close()  # coroutine non attendue (injoignable) : pas d'avertissement

    def _duration(self, transition: int | None) -> int:
        return self.transition if transition is None else transition

    def _effect(self, transition: int | None) -> dict:
        """Transition en millisecondes (0 = immédiat) au format de la bibliothèque."""
        duration = self._duration(transition)
        if duration <= 0:
            return {"effect": "sudden"}
        return {"effect": "smooth", "duration": max(30, duration)}


Driver = YeelightBulb  # convention : le chargeur cherche ce nom
