"""Pilote WLED : API JSON par WebSocket (/ws), l'état est poussé à chaque changement."""

import asyncio
import json
import logging

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from core.base import DeviceError, IPDevice, NetworkDevice, Switchable

log = logging.getLogger(__name__)

ERRORS = (OSError, TimeoutError, WebSocketException)


class WLED(Switchable, IPDevice, NetworkDevice):
    """Contrôleur WLED : connexion permanente, l'état vient de ses messages."""

    WIDGET = "onoff"
    BRAND = "WLED"

    def __init__(self, host: str, port: int = 80, retry: float = 10):
        super().__init__(host, port)
        self._retry = retry
        self._ws: ClientConnection | None = None


    async def start(self) -> None:
        self._spawn(self._run())

    # --- connexion ----------------------------------------------------------

    async def _run(self) -> None:
        # Le WLED envoie son état complet à la connexion puis à chaque changement ; les pings
        # de websockets détectent une coupure.
        while True:
            reason = ""
            try:
                async with connect(f"ws://{self._host}:{self._port}/ws", open_timeout=5) as ws:
                    self._ws = ws
                    self._set_connected(True)
                    async for message in ws:
                        self._on_message(message)
            except ERRORS as err:
                reason = str(err)
            self._ws = None
            self._set_connected(False, reason=reason)
            await asyncio.sleep(self._retry)

    def _on_message(self, message: str | bytes) -> None:
        state = json.loads(message).get("state")
        if state is None:
            return  # autre message (ex. réponse "success")
        WLED.on.update(self, state["on"])
        self._set_connected(True, synced=True)

    # --- commandes ----------------------------------------------------------

    async def _switch(self, on: bool, transition: int | None) -> None:
        self._check_reachable()
        assert self._ws is not None  # connecté
        try:
            await self._ws.send(json.dumps({"on": on}))
        except ERRORS as err:
            raise DeviceError(f"{self.id} : {err}") from err
        WLED.on.update(self, on)


Driver = WLED  # convention : le chargeur cherche ce nom
