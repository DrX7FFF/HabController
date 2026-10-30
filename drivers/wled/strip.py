"""Pilote WLED : API JSON par WebSocket (/ws), l'état est poussé à chaque changement."""

import asyncio
import contextlib
import json
import logging

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from core.base import Attribute, DeviceError, NetworkDevice, on_value

log = logging.getLogger(__name__)

ERRORS = (OSError, TimeoutError, WebSocketException)


class WLED(NetworkDevice):
    """Contrôleur WLED : connexion permanente, l'état vient de ses messages."""

    on = Attribute[bool]()  # True / False ; "toggle" accepté en écriture

    def __init__(self, host: str, port: int = 80, retry: float = 10):
        super().__init__()
        self._host = host
        self._port = port
        self._retry = retry
        self._ws: ClientConnection | None = None
        self._on: bool | None = None
        self._task: asyncio.Task | None = None

    def physical(self) -> str:
        return f"WLED {self._host}"

    def details(self) -> dict:
        return {"host": self._host}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # --- connexion ----------------------------------------------------------

    async def _run(self) -> None:
        # Le WLED envoie son état complet à la connexion puis à chaque changement ; les pings
        # de websockets détectent une coupure.
        while True:
            try:
                async with connect(f"ws://{self._host}:{self._port}/ws", open_timeout=5) as ws:
                    self._ws = ws
                    self._connected = True
                    async for message in ws:
                        self._on_message(message)
            except ERRORS as err:
                if self._connected:
                    log.warning("%s : connexion perdue (%s)", self.id, err)
            self._ws = None
            self._connected = self._synced = False
            self._notify()
            await asyncio.sleep(self._retry)

    def _on_message(self, message: str | bytes) -> None:
        state = json.loads(message).get("state")
        if state is None:
            return  # autre message (ex. réponse "success")
        self._on = state["on"]
        self._synced = True
        self._notify()

    def _get_connected_state(self) -> dict:
        return {"on": self._on}

    # --- commandes ----------------------------------------------------------

    async def set(self, name: str, value: bool | str | int, transition: int | None = None) -> None:
        if name != "on":
            raise ValueError(f"{self.id} ne gère pas « {name} »")
        on = on_value(value, self._on)
        if self._ws is None or not self._synced:
            raise DeviceError(f"{self.id} : injoignable")
        try:
            await self._ws.send(json.dumps({"on": on}))
        except ERRORS as err:
            raise DeviceError(f"{self.id} : {err}") from err
        self._on = on
        self._notify()


Driver = WLED  # convention : le chargeur cherche ce nom
