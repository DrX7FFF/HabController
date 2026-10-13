import asyncio
import contextlib
import logging

from yeelight import BulbException
from yeelight.aio import KEY_CONNECTED, AsyncBulb

from core.base import Attribute, NetworkDevice, DeviceError

log = logging.getLogger(__name__)

ERRORS = (BulbException, OSError)  # erreurs du protocole ou du réseau
PROPERTIES = ["power", "bright", "ct", "rgb"]
SETTABLE = frozenset({"power", "brightness", "color_temp", "rgb"})
RETRY_DELAY = 10  # s, entre deux tentatives de première connexion


def _effect(transition: int) -> dict:
    """Transition en millisecondes (0 = immédiat) au format de la bibliothèque."""
    if transition <= 0:
        return {"effect": "sudden"}
    return {"effect": "smooth", "duration": max(30, transition)}


class YeelightBulb(NetworkDevice):
    """Ampoule Yeelight : une connexion permanente, l'état vient des notifications."""

    power = Attribute[str]()  # "on", "off", "toggle" (écriture) ; "unreachable" en lecture si disconnecté
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

    def _get_disconnected_state(self) -> dict:
        return {"power": "unreachable"}

    def _get_connected_state(self) -> dict:
        state = {"power": self._props.get("power", "unreachable")}
        if "bright" in self._props:
            state["brightness"] = int(self._props["bright"])
        if "ct" in self._props:
            state["color_temp"] = int(self._props["ct"])
        if "rgb" in self._props:
            state["rgb"] = f"#{int(self._props['rgb']):06x}"
        return state

    # --- commandes ----------------------------------------------------------

    async def set(self, name: str, value: str | int, transition: int | None = None) -> None:
        if name not in SETTABLE:
            raise ValueError(f"{self.name} ne gère pas « {name} »")
        if not (self._connected and self._synced):
            raise DeviceError(f"{self.name} : injoignable")
        fx = _effect(self.transition if transition is None else transition)
        try:
            await self._apply(name, value, fx)
        except ERRORS as err:
            raise DeviceError(f"{self.name} : {err}") from err
        self._notify()

    async def _apply(self, name: str, value: str | int, fx: dict) -> None:
        bulb = self._bulb
        if name == "power":
            if value == "toggle":
                value = "off" if self._props.get("power") == "on" else "on"
            if value == "on":
                await bulb.async_turn_on(**fx)
            elif value == "off":
                await bulb.async_turn_off(**fx)
            else:
                raise ValueError("power attend « on », « off » ou « toggle »")
            self._props["power"] = value
        elif name == "brightness":
            await bulb.async_set_brightness(int(value), **fx)
            self._props["bright"] = str(int(value))
        elif name == "color_temp":
            await bulb.async_set_color_temp(int(value), **fx)
            self._props["ct"] = str(int(value))
        elif name == "rgb":
            text = str(value).lstrip("#")
            if len(text) != 6:
                raise ValueError("rgb attend une couleur au format #rrggbb")
            red, green, blue = (int(text[i : i + 2], 16) for i in (0, 2, 4))
            await bulb.async_set_rgb(red, green, blue, **fx)
            self._props["rgb"] = str((red << 16) | (green << 8) | blue)


Driver = YeelightBulb  # convention : le chargeur cherche ce nom
