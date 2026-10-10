import threading

from yeelight import Bulb, BulbException

from .base import Device, DeviceError

ERRORS = (BulbException, OSError)  # erreurs du protocole ou du réseau
PROPERTIES = ["power", "bright", "ct", "rgb"]


def _effect(transition: int) -> dict:
    """Transition en millisecondes (0 = immédiat) au format de la bibliothèque."""
    if transition <= 0:
        return {"effect": "sudden"}
    return {"effect": "smooth", "duration": max(30, transition)}


class YeelightBulb(Device):
    capabilities = frozenset({"power", "brightness", "color_temp", "rgb"})

    def __init__(
        self,
        id: str,
        name: str,
        host: str,
        port: int = 55443,
        transition: int = 300,
        capabilities: list[str] | None = None,
    ):
        super().__init__(id, name, capabilities)
        self.transition = transition  # durée par défaut des transitions (ms)
        self._bulb = Bulb(host, port, auto_on=False)
        self._lock = threading.Lock()  # une commande à la fois sur la connexion

    def state(self) -> dict:
        try:
            with self._lock:
                props = self._bulb.get_properties(PROPERTIES)
        except ERRORS:
            return {"power": "unreachable"}
        state = {"power": props.get("power") or "unreachable"}
        if "brightness" in self.capabilities and props.get("bright"):
            state["brightness"] = int(props["bright"])
        if "color_temp" in self.capabilities and props.get("ct"):
            state["color_temp"] = int(props["ct"])
        if "rgb" in self.capabilities and props.get("rgb"):
            state["rgb"] = f"#{int(props['rgb']):06x}"
        return state

    def set(self, name: str, value: str | int, transition: int | None = None) -> None:
        if name not in self.capabilities:
            raise ValueError(f"{self.name} ne gère pas « {name} »")
        fx = _effect(self.transition if transition is None else transition)
        try:
            with self._lock:
                self._apply(name, value, fx)
        except ERRORS as err:
            raise DeviceError(f"{self.name} : {err}") from err

    def _apply(self, name: str, value: str | int, fx: dict) -> None:
        bulb = self._bulb
        if name == "power":
            if value == "toggle":
                value = "off" if bulb.get_properties(["power"])["power"] == "on" else "on"
            if value == "on":
                bulb.turn_on(**fx)
            elif value == "off":
                bulb.turn_off(**fx)
            else:
                raise ValueError("power attend « on », « off » ou « toggle »")
        elif name == "brightness":
            bulb.set_brightness(int(value), **fx)
        elif name == "color_temp":
            bulb.set_color_temp(int(value), **fx)
        elif name == "rgb":
            text = str(value).lstrip("#")
            if len(text) != 6:
                raise ValueError("rgb attend une couleur au format #rrggbb")
            red, green, blue = (int(text[i : i + 2], 16) for i in (0, 2, 4))
            bulb.set_rgb(red, green, blue, **fx)
