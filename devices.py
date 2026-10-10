import threading
import tomllib
from pathlib import Path

from yeelight import Bulb


class YeelightBulb:
    def __init__(self, id: str, name: str, host: str, port: int = 55443):
        self.id = id
        self.name = name
        self._bulb = Bulb(host, port, auto_on=False)
        self._lock = threading.Lock()  # une commande à la fois sur la connexion

    def state(self) -> dict:
        try:
            with self._lock:
                return {"power": self._bulb.get_properties()["power"]}
        except Exception:
            return {"power": "unreachable"}

    def toggle(self) -> None:
        with self._lock:
            self._bulb.toggle()


DRIVERS = {"yeelight": YeelightBulb}


def load_devices(path: str = "devices.toml") -> dict:
    conf = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    devices = {}
    for dev_id, params in conf.items():
        params = dict(params)
        driver = DRIVERS[params.pop("driver")]
        devices[dev_id] = driver(id=dev_id, **params)
    return devices
