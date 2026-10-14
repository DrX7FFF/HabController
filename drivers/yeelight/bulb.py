import asyncio
import contextlib
import logging

from yeelight import BulbException, Flow, RGBTransition, SceneClass, TemperatureTransition
from yeelight.aio import KEY_CONNECTED, AsyncBulb

from core.base import Attribute, DeviceError, NetworkDevice, on_value

log = logging.getLogger(__name__)

ERRORS = (BulbException, OSError)  # erreurs du protocole ou du réseau
PROPERTIES = ["power", "bright", "ct", "rgb"]
SETTABLE = frozenset({"on", "brightness", "color_temp", "rgb"})
RETRY_DELAY = 10  # s, entre deux tentatives de première connexion


def _rgb(value: str) -> tuple[int, int, int]:
    text = str(value).lstrip("#")
    if len(text) != 6:
        raise ValueError("rgb attend une couleur au format #rrggbb")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _effect(transition: int) -> dict:
    """Transition en millisecondes (0 = immédiat) au format de la bibliothèque."""
    if transition <= 0:
        return {"effect": "sudden"}
    return {"effect": "smooth", "duration": max(30, transition)}


class YeelightBulb(NetworkDevice):
    """Ampoule Yeelight : une connexion permanente, l'état vient des notifications."""

    on = Attribute[bool]()  # True / False ; "toggle" accepté en écriture
    brightness = Attribute[int]()  # 1 à 100
    color_temp = Attribute[int]()  # Kelvin
    rgb = Attribute[str]()  # "#rrggbb"

    def __init__(
        self,
        host: str,
        port: int = 55443,
        transition: int = 300,
        name: str = "",
    ):
        super().__init__(name)
        self.transition = transition  # durée par défaut des transitions (ms)
        self._host = host
        self._port = port
        self._bulb: AsyncBulb | None = None
        self._props: dict = {}  # dernières valeurs connues, noms du protocole Yeelight
        self._task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None

    # --- cycle de vie -------------------------------------------------------

    async def start(self) -> None:
        # AsyncBulb a besoin de la boucle d'événements en cours : création ici, pas dans __init__.
        self._bulb = AsyncBulb(self._host, self._port, auto_on=False)
        self._task = asyncio.create_task(self._connect())

    async def stop(self) -> None:
        for task in (self._task, self._refresh_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._bulb:
            await self._bulb.async_stop_listening()

    async def _connect(self) -> None:
        # Après une première connexion réussie, la bibliothèque se reconnecte seule.
        while True:
            try:
                await self._bulb.async_listen(self._on_update)
            except ERRORS as err:
                log.warning("%s : connexion impossible (%s), nouvel essai", self.name, err)
                await asyncio.sleep(RETRY_DELAY)
            else:
                return

    # --- état ---------------------------------------------------------------

    def _on_update(self, props: dict) -> None:
        """Appelé par la bibliothèque : changement d'état ou de connexion."""
        # Chaque notification porte aussi KEY_CONNECTED=True : on ne réagit qu'aux changements.
        connected = props.get(KEY_CONNECTED, self._connected)
        if connected != self._connected:
            self._connected = connected
            self._synced = False
            if connected:  # l'état a pu changer pendant la coupure
                self._refresh_task = asyncio.get_running_loop().create_task(self._refresh())
        self._merge(props)
        self._notify()

    def _merge(self, props: dict) -> None:
        for key in PROPERTIES:
            if props.get(key) not in (None, ""):
                self._props[key] = props[key]

    async def _refresh(self) -> None:
        try:
            props = await self._bulb.async_get_properties(PROPERTIES)
        except ERRORS as err:
            log.warning("%s : lecture de l'état impossible (%s)", self.name, err)
            return
        self._merge(props)
        self._synced = True
        self._notify()

    def _get_connected_state(self) -> dict:
        state = {}
        if "power" in self._props:  # le protocole Yeelight dit "on" / "off"
            state["on"] = self._props["power"] == "on"
        if "bright" in self._props:
            state["brightness"] = int(self._props["bright"])
        if "ct" in self._props:
            state["color_temp"] = int(self._props["ct"])
        if "rgb" in self._props:
            state["rgb"] = f"#{int(self._props['rgb']):06x}"
        return state

    # --- commandes ----------------------------------------------------------
    # toggle, set_rgb et set_temp : en tâche de fond, comme salon.on = True. set_rgb et
    # set_temp allument et règlent tout en un seul message (scène « color flow » d'une étape,
    # la seule qui accepte une transition).

    def toggle(self, transition: int | None = None) -> None:
        self._background("toggle", lambda: self.set("on", "toggle", transition))

    def set_rgb(self, brightness: int, rgb: str, transition: int | None = None) -> None:
        self._background(
            f"set_rgb({brightness}, {rgb!r})", lambda: self._set_rgb(brightness, rgb, transition)
        )

    def set_temp(self, brightness: int, color_temp: int, transition: int | None = None) -> None:
        self._background(
            f"set_temp({brightness}, {color_temp})",
            lambda: self._set_temp(brightness, color_temp, transition),
        )

    async def _set_rgb(self, brightness: int, rgb: str, transition: int | None) -> None:
        red, green, blue = _rgb(rgb)
        step = RGBTransition(red, green, blue, self._duration(transition), int(brightness))
        rgb_value = str((red << 16) | (green << 8) | blue)  # format du protocole
        await self._scene(step, bright=str(int(brightness)), rgb=rgb_value)

    async def _set_temp(self, brightness: int, color_temp: int, transition: int | None) -> None:
        step = TemperatureTransition(int(color_temp), self._duration(transition), int(brightness))
        await self._scene(step, bright=str(int(brightness)), ct=str(int(color_temp)))

    async def _scene(self, step, **props: str) -> None:
        self._check_reachable()
        try:
            await self._bulb.async_set_scene(SceneClass.CF, Flow(1, Flow.actions.stay, [step]))
        except ERRORS as err:
            raise DeviceError(f"{self.name} : {err}") from err
        self._props.update(power="on", **props)
        self._notify()

    def _duration(self, transition: int | None) -> int:
        return self.transition if transition is None else transition

    def _check_reachable(self) -> None:
        if not (self._connected and self._synced):
            raise DeviceError(f"{self.name} : injoignable")

    async def set(self, name: str, value: bool | str | int, transition: int | None = None) -> None:
        if name not in SETTABLE:
            raise ValueError(f"{self.name} ne gère pas « {name} »")
        self._check_reachable()
        fx = _effect(self._duration(transition))
        try:
            await self._apply(name, value, fx)
        except ERRORS as err:
            raise DeviceError(f"{self.name} : {err}") from err
        self._notify()

    async def _apply(self, name: str, value: bool | str | int, fx: dict) -> None:
        bulb = self._bulb
        if name == "on":
            on = on_value(value, self._props.get("power") == "on")
            if on:
                await bulb.async_turn_on(**fx)
            else:
                await bulb.async_turn_off(**fx)
            self._props["power"] = "on" if on else "off"
        elif name == "brightness":
            await bulb.async_set_brightness(int(value), **fx)
            self._props["bright"] = str(int(value))
        elif name == "color_temp":
            await bulb.async_set_color_temp(int(value), **fx)
            self._props["ct"] = str(int(value))
        elif name == "rgb":
            red, green, blue = _rgb(str(value))
            await bulb.async_set_rgb(red, green, blue, **fx)
            self._props["rgb"] = str((red << 16) | (green << 8) | blue)


Driver = YeelightBulb  # convention : le chargeur cherche ce nom
