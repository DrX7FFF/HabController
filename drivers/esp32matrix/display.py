"""Pilote afficheur ESP32RGBMatrix : lignes de texte sur TCP (port 5000), l'état est poussé à
chaque changement (« state on=1 auto=1 pattern="Plasma" »)."""

import asyncio
import logging
import shlex

from core.base import (
    DeviceError,
    EventBool,
    EventSource,
    EventStr,
    IPDevice,
    NetworkDevice,
    State,
    Switchable,
)

log = logging.getLogger(__name__)


class RGBMatrix(Switchable, IPDevice, NetworkDevice):
    """Afficheur HUB75 : connexion permanente, l'état vient de ses messages."""

    WIDGET = "matrix"
    BRAND = "RGBMatrix"
    auto = State[bool]()  # enchaînement automatique des animations (False : pause)
    pattern = State[str]()  # animation affichée
    event_auto = EventSource[EventBool]()
    event_pattern = EventSource[EventStr]()

    def __init__(self, host: str, port: int = 5000, retry: float = 10, ping: float = 10):
        super().__init__(host, port)
        self._retry = retry
        self._ping = ping
        self._writer: asyncio.StreamWriter | None = None


    async def start(self) -> None:
        self._spawn(self._run())

    # --- connexion ----------------------------------------------------------

    async def _run(self) -> None:
        # L'afficheur envoie son état à la connexion puis à chaque changement ; sans message
        # pendant `ping` secondes, un « ping » est envoyé, et sans réponse la connexion est coupée.
        while True:
            reason = ""
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port), 5
                )
                self._writer = writer
                self._set_connected(True)
                try:
                    await self._read(reader)
                finally:
                    self._writer = None
                    writer.close()
            except (OSError, TimeoutError) as err:
                reason = str(err) or type(err).__name__
            self._set_connected(False, reason=reason)
            await asyncio.sleep(self._retry)

    async def _read(self, reader: asyncio.StreamReader) -> None:
        waiting = False
        while True:
            try:
                line = await asyncio.wait_for(reader.readline(), self._ping)
            except TimeoutError:
                if waiting:
                    raise TimeoutError("pas de réponse au ping") from None
                await self._send("ping")
                waiting = True
                continue
            if not line:
                raise ConnectionError("connexion fermée")
            waiting = False
            self._on_line(line.decode(errors="replace").strip())

    def _on_line(self, line: str) -> None:
        if not line.startswith("state "):
            return  # autre réponse (pong, messages)
        try:
            words = shlex.split(line)[1:]
        except ValueError:
            return  # guillemet non fermé
        fields = dict(f.split("=", 1) for f in words if "=" in f)
        if "on" not in fields:
            return
        RGBMatrix.on.update(self, fields["on"] == "1")
        RGBMatrix.auto.update(self, fields["auto"] == "1" if "auto" in fields else None)
        RGBMatrix.pattern.update(self, fields.get("pattern"))
        self._set_connected(True, synced=True)

    async def _send(self, command: str) -> None:
        assert self._writer is not None  # connecté
        self._writer.write(f"{command}\n".encode())
        await self._writer.drain()

    # --- actions : l'afficheur renvoie son état après chaque commande -------

    def set_auto(self, auto: bool) -> None:
        """Enchaînement automatique des animations (False : pause)."""
        self._background(f"set_auto({auto!r})", lambda: self._command("play" if auto else "pause"))

    def next_pattern(self) -> None:
        self._background("next_pattern", lambda: self._command("n"))

    def previous_pattern(self) -> None:
        self._background("previous_pattern", lambda: self._command("p"))

    async def _switch(self, on: bool, transition: int | None) -> None:
        await self._command("on" if on else "off")

    async def _command(self, command: str) -> None:
        self._check_reachable()
        try:
            await self._send(command)
        except OSError as err:
            raise DeviceError(f"{self.id} : {err}") from err


Driver = RGBMatrix  # convention : le chargeur cherche ce nom
