"""Pilote afficheur ESP32RGBMatrix : lignes de texte sur TCP (port 5000), l'état est poussé à
chaque changement (« state on=1 auto=1 pattern="Plasma" »)."""

import asyncio
import shlex
import logging

from core.base import Attribute, DeviceError, EventBool, EventSource, NetworkDevice, on_value

log = logging.getLogger(__name__)


class RGBMatrix(NetworkDevice):
    """Afficheur HUB75 : connexion permanente, l'état vient de ses messages."""

    WIDGET = "matrix"
    on = Attribute[bool]()
    auto = Attribute[bool]()  # enchaînement automatique des animations (False : pause)
    pattern = Attribute[str]()  # animation affichée ; commande "next" ou "prev"
    event_on = EventSource[EventBool]()

    def __init__(self, host: str, port: int = 5000, retry: float = 10, ping: float = 10):
        super().__init__()
        self._host = host
        self._port = port
        self._retry = retry
        self._ping = ping
        self._writer: asyncio.StreamWriter | None = None
        self._on: bool | None = None
        self._auto: bool | None = None
        self._pattern: str | None = None

    def physical(self) -> str:
        return f"RGBMatrix {self._host}"

    def details(self) -> dict:
        return {"host": self._host}

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
        self._on = fields["on"] == "1"
        self._auto = fields["auto"] == "1" if "auto" in fields else None
        self._pattern = fields.get("pattern")
        self._synced = True
        self._notify()

    async def _send(self, command: str) -> None:
        assert self._writer is not None  # connecté
        self._writer.write(f"{command}\n".encode())
        await self._writer.drain()

    def _get_connected_state(self) -> dict:
        return {"on": self._on, "auto": self._auto, "pattern": self._pattern}

    # --- commandes ----------------------------------------------------------

    def toggle(self) -> None:
        self._background("toggle", lambda: self.set("on", not self.on))

    async def set(self, name: str, value: bool | str | int, transition: int | None = None) -> None:
        # l'afficheur renvoie son état après chaque commande : pas de mise à jour locale
        if name == "on":
            command = "on" if on_value(value) else "off"
        elif name == "auto":
            command = "play" if on_value(value) else "pause"
        elif name == "pattern" and value in ("next", "prev"):
            command = "n" if value == "next" else "p"
        else:
            raise ValueError(f"{self.id} ne gère pas « {name} » = {value!r}")
        self._check_reachable()
        try:
            await self._send(command)
        except OSError as err:
            raise DeviceError(f"{self.id} : {err}") from err


Driver = RGBMatrix  # convention : le chargeur cherche ce nom
