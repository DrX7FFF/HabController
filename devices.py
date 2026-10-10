import tomllib
from pathlib import Path

from drivers import DRIVERS


def load_devices(path: str = "devices.toml") -> dict:
    conf = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    devices = {}
    for dev_id, params in conf.items():
        params = dict(params)
        driver_name = params.pop("driver")
        if driver_name not in DRIVERS:
            raise ValueError(f"{dev_id} : driver inconnu « {driver_name} »")
        devices[dev_id] = DRIVERS[driver_name](id=dev_id, **params)
    return devices
