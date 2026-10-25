"""Historique des valeurs dans Firebase."""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from functools import partial

import aiohttp
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials

from core import CONFIG_DIR, settings
from core.base import Device, spawn_task

log = logging.getLogger(__name__)
__all__ = ["record"]

SCOPES = [
    "https://www.googleapis.com/auth/firebase.database",
    "https://www.googleapis.com/auth/userinfo.email",
]
UNITS = {"h": 3600, "d": 86400, "y": 365 * 86400}
PURGE_PERIOD = 86400  # s
TIMEOUT = aiohttp.ClientTimeout(total=10)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def duration_ms(text: str) -> int:
    """« 48h », « 30d », « 1y » en millisecondes."""
    match = re.fullmatch(r"(\d+)([hdy])", str(text).strip())
    if not match:
        raise ValueError(f"datalog : durée invalide « {text} » (ex. 48h, 30d, 1y)")
    return int(match[1]) * UNITS[match[2]] * 1000


class Datalog:
    def __init__(self, conf: dict, devices: dict[str, Device]):
        self.url = conf["url"].rstrip("/")
        self.credentials_file = CONFIG_DIR / conf["credentials"]
        self.default_retention = duration_ms(conf.get("retention", "30d"))
        self.retention: dict[str, int] = {  # chemin -> rétention (ms)
            path: duration_ms(value) for path, value in conf.get("purge", {}).items()
        }
        self.watched: dict[str, dict[str, dict]] = {}  # équipement -> attribut -> réglages
        for dev_id, attrs in conf.get("devices", {}).items():
            if dev_id not in devices:
                raise ValueError(f"datalog : équipement inconnu « {dev_id} »")
            self.watched[dev_id] = attrs
            for attr, options in attrs.items():
                retention = options.get("retention")
                self.retention[device_path(dev_id, attr)] = (
                    duration_ms(retention) if retention else self.default_retention
                )
        self.last: dict[str, object] = {}  # chemin -> dernière valeur enregistrée
        self._credentials: Credentials | None = None
        self._token_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._purge_task: asyncio.Task | None = None
        self._failing = False
        self._listeners: list[Callable[[list], None]] = []
        for dev_id in self.watched:
            devices[dev_id].subscribe(partial(self.on_state, devices[dev_id]))

    # --- cycle de vie -------------------------------------------------------

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=TIMEOUT)
        self._purge_task = asyncio.create_task(self._purge_loop())

    async def stop(self) -> None:
        if self._purge_task:
            self._purge_task.cancel()
            await asyncio.gather(self._purge_task, return_exceptions=True)
        if self._session:
            await self._session.close()

    # --- enregistrement -----------------------------------------------------

    def on_state(self, device: Device, state: dict) -> None:
        at = device.changed_at or now_ms()
        data = {}
        for attr, options in self.watched[device.id].items():
            if attr not in state:  # injoignable, ou pas encore lu
                continue
            path, value = device_path(device.id, attr), state[attr]
            if self._should_record(path, value, options.get("hysteresis")):
                self.last[path] = value
                data[f"{path}/{at}"] = value
        if data:
            self.send(data)

    def _should_record(self, path: str, value, hysteresis) -> bool:
        if path not in self.last:
            return True
        last = self.last[path]
        if hysteresis and is_number(value) and is_number(last):
            return abs(value - last) >= hysteresis
        return value != last

    def record(self, path: str, value, at: int | None) -> None:
        path = path.strip("/")
        self.retention.setdefault(path, self.default_retention)
        self.send({f"{path}/{now_ms() if at is None else at}": value})

    def subscribe(self, callback: Callable[[list], None]) -> None:
        """callback([[chemin, ms, valeur], ...]) à chaque enregistrement (graphiques en direct)."""
        self._listeners.append(callback)

    def send(self, data: dict) -> None:
        points = [[*key.rsplit("/", 1), value] for key, value in data.items()]
        for point in points:
            point[1] = int(point[1])
        for callback in self._listeners:
            callback(points)
        spawn_task(self._patch(data), "datalog")

    # --- Firebase -----------------------------------------------------------

    async def _token(self) -> str:
        async with self._token_lock:
            if self._credentials is None:
                self._credentials = Credentials.from_service_account_file(
                    str(self.credentials_file), scopes=SCOPES
                )
            if not self._credentials.valid:  # jeton valable une heure
                await asyncio.to_thread(self._credentials.refresh, Request())
            return self._credentials.token

    async def _request(self, method: str, path: str, **kwargs):
        assert self._session is not None, "Datalog.start() non appelé"
        headers = {"Authorization": f"Bearer {await self._token()}"}
        url = f"{self.url}/{path}.json"
        async with self._session.request(method, url, headers=headers, **kwargs) as response:
            response.raise_for_status()
            return await response.json()

    async def _patch(self, data: dict) -> None:
        """Écrit plusieurs chemins en une requête (PATCH à la racine)."""
        try:
            await self._request("PATCH", "", json=data)
        except Exception as err:  # réseau, clé, droits : on perd la valeur, sans insister
            if not self._failing:
                log.warning("datalog : envoi impossible (%s), valeurs perdues", describe(err))
            self._failing = True
            return
        if self._failing:
            log.info("datalog : envoi rétabli")
            self._failing = False

    async def read(self, path: str, start: int, end: int) -> list[list]:
        """Points [ms, valeur] de path entre start et end (ms), triés. Le premier donne l'état
        au début de la période : la dernière valeur d'avant, ramenée à start."""
        path = path.strip("/")
        inside = await self._request(
            "GET", path, params={"orderBy": '"$key"', "startAt": f'"{start}"', "endAt": f'"{end}"'}
        )
        before = await self._request(
            "GET", path, params={"orderBy": '"$key"', "endAt": f'"{start - 1}"', "limitToLast": "1"}
        )
        points = sorted([int(key), value] for key, value in (inside or {}).items())
        if before and (not points or points[0][0] > start):
            points.insert(0, [start, next(iter(before.values()))])
        return points

    async def purge(self) -> None:
        """Supprime, chemin par chemin, les valeurs plus vieilles que leur rétention."""
        for path, retention in list(self.retention.items()):
            limit = now_ms() - retention
            params = {"orderBy": '"$key"', "endAt": f'"{limit}"'}
            old = await self._request("GET", path, params=params) or {}
            if old:
                await self._request("PATCH", path, json=dict.fromkeys(old))
                log.info("datalog : %s, %d valeurs purgées", path, len(old))

    async def _purge_loop(self) -> None:
        while True:
            try:
                await self.purge()
            except Exception as err:
                log.warning("datalog : purge impossible (%s)", describe(err))
            await asyncio.sleep(PURGE_PERIOD)


def device_path(dev_id: str, attr: str) -> str:
    return f"devices/{dev_id}/{attr.replace('.', '/')}"


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def describe(err: Exception) -> str:
    return str(err) or type(err).__name__


_datalog: Datalog | None = None


def setup(devices: dict[str, Device]) -> Datalog | None:
    """Lit [datalog] de habctrl.toml et s'abonne aux équipements ; None si non configuré."""
    global _datalog
    conf = settings("datalog")
    _datalog = Datalog(conf, devices) if conf.get("url") else None
    return _datalog


def record(path: str, value, *, at: int | None = None) -> None:
    """Enregistre value sous <path>/<at> (ms ; maintenant par défaut). Ne bloque pas."""
    if _datalog is None:
        log.debug("datalog non configuré : %s ignoré", path)
        return
    _datalog.record(path, value, at)
