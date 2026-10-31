"""Pilote TV Samsung d'avant 2016 (protocole « legacy »)."""

import asyncio
import base64
import contextlib
import logging
import socket

from core.base import Attribute, DeviceError, NetworkDevice, on_value

log = logging.getLogger(__name__)

APP = "HabController"  # nom sous lequel la TV autorise la télécommande
ACCESS_DENIED = b"\x64\x00\x00\x00"


def _string(data: bytes, encode: bool = True) -> bytes:
    """Chaîne du protocole : longueur sur 2 octets (petit-boutiste) puis contenu (en base64)."""
    if encode:
        data = base64.b64encode(data)
    return len(data).to_bytes(2, "little") + data


def _packet(payload: bytes) -> bytes:
    return b"\x00" + _string(b"", encode=False) + _string(payload, encode=False)


def auth_packet() -> bytes:
    name = APP.encode()
    return _packet(b"\x64\x00" + _string(name) + _string(name) + _string(name))


def key_packet(key: str) -> bytes:
    return _packet(b"\x00\x00\x00" + _string(key.encode()))


async def read_response(reader: asyncio.StreamReader) -> bytes:
    """Réponse de la TV : en-tête (type, nom de l'appli) puis contenu."""
    header = await reader.readexactly(3)
    await reader.readexactly(int.from_bytes(header[1:3], "little"))
    size = int.from_bytes(await reader.readexactly(2), "little")
    return await reader.readexactly(size)


class SamsungTV(NetworkDevice):
    """TV Samsung legacy : connexion permanente, allumée tant qu'elle répond."""

    WIDGET = "onoff"
    on = Attribute[bool]()  # écriture : False seulement

    def __init__(self, host: str, port: int = 55000, retry: float = 30):
        super().__init__()
        self._host = host
        self._port = port
        self._retry = retry
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    def physical(self) -> str:
        return f"Samsung {self._host}"

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
        while True:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port), 5
                )
            except (OSError, TimeoutError):
                await asyncio.sleep(self._retry)
                continue
            try:
                await self._serve(reader, writer)
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                self._writer = None
                writer.close()
                if self._connected:
                    log.info("%s : déconnecté", self.id)
                self._set_connected(False)
            await asyncio.sleep(self._retry)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        writer.write(auth_packet())
        await writer.drain()
        log.info("%s : connecté", self.id)
        self._writer = writer
        self._set_connected(True)
        while True:  # réponses de la TV, jusqu'à la fermeture de la connexion
            response = await read_response(reader)
            if response == ACCESS_DENIED:
                log.error("%s : « %s » refusé par la TV (réglages de la TV)", self.id, APP)

    def _set_connected(self, connected: bool) -> None:
        self._connected = self._synced = connected
        self._notify()

    def _get_connected_state(self) -> dict:
        return {"on": True}

    def _get_disconnected_state(self) -> dict:
        return {"on": False}

    # --- commandes ----------------------------------------------------------

    async def set(self, name: str, value: bool | str | int, transition: int | None = None) -> None:
        if name != "on":
            raise ValueError(f"{self.id} ne gère pas « {name} »")
        if on_value(value, self._connected):
            raise ValueError(f"{self.id} : impossible de l'allumer par le réseau")
        if self._writer is None:
            return  # déjà éteinte
        try:
            self._writer.write(key_packet("KEY_POWEROFF"))
            await self._writer.drain()
        except OSError as err:
            raise DeviceError(f"{self.id} : {err}") from err


Driver = SamsungTV  # convention : le chargeur cherche ce nom
