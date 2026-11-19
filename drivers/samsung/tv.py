"""Pilote TV Samsung d'avant 2016 (protocole « legacy »)."""

import asyncio
import base64
import logging
import socket
from typing import override

from core.base import DeviceError, IPDevice, NetworkDevice, Switchable

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


class SamsungTV(Switchable, IPDevice, NetworkDevice):
    """TV Samsung legacy : connexion permanente, allumée tant qu'elle répond (`on` suit la
    connexion, False quand elle ne répond pas). Ne peut être qu'éteinte par le réseau."""

    WIDGET = "onoff"
    BRAND = "Samsung"

    def __init__(self, host: str, port: int = 55000, retry: float = 30):
        super().__init__(host, port)
        self._retry = retry
        self._writer: asyncio.StreamWriter | None = None
        SamsungTV.on.update(self, False)

    @override
    async def start(self) -> None:
        self._spawn(self._run())

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
                if self._connected:  # TV éteinte : fonctionnement normal, pas un avertissement
                    log.info("%s : déconnecté", self.id)
                self._set_connected(False, silent=True)
            await asyncio.sleep(self._retry)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        writer.write(auth_packet())
        await writer.drain()
        self._writer = writer
        self._set_connected(True, synced=True)
        SamsungTV.on.update(self, True)
        while True:  # réponses de la TV, jusqu'à la fermeture de la connexion
            response = await read_response(reader)
            if response == ACCESS_DENIED:
                log.error("%s : « %s » refusé par la TV (réglages de la TV)", self.id, APP)

    @override
    def _reset(self) -> None:
        super()._reset()
        SamsungTV.on.update(self, False)  # éteinte : on vaut False, pas None

    # --- commandes ----------------------------------------------------------

    @override
    async def _switch(self, on: bool, transition: int | None) -> None:
        if on:
            raise ValueError(f"{self.id} : impossible de l'allumer par le réseau")
        if self._writer is None:
            return  # déjà éteinte
        try:
            self._writer.write(key_packet("KEY_POWEROFF"))
            await self._writer.drain()
        except OSError as err:
            raise DeviceError(f"{self.id} : {err}") from err


Driver = SamsungTV  # convention : le chargeur cherche ce nom
