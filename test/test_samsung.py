"""Tests TV Samsung sans TV : une fausse TV locale (serveur TCP)."""

import asyncio
import os
import sys
import unittest
from pathlib import Path

# configuration de test (test/config/), jamais celle de l'installation
os.environ["HABCTRL_CONFIG"] = str(Path(__file__).resolve().parent / "config")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drivers.samsung.tv import SamsungTV, auth_packet, key_packet  # noqa: E402


async def until(condition, limit=2.0):
    async with asyncio.timeout(limit):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.005)  # noqa: ASYNC110


class SamsungTVTest(unittest.IsolatedAsyncioTestCase):
    async def test_state_follows_connection_and_power_off(self):
        received = bytearray()
        clients = []

        async def fake_tv(reader, writer):
            clients.append(writer)
            while data := await reader.read(1024):
                received.extend(data)

        server = await asyncio.start_server(fake_tv, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        tv = SamsungTV(host="127.0.0.1", port=port, retry=0.05)
        self.assertEqual(tv.state(), {"connected": False, "on": False})
        await tv.start()
        await until(lambda: tv.on)
        self.assertEqual(tv.state(), {"connected": True, "on": True})
        with self.assertRaises(ValueError):
            await tv.set("on", True)  # impossible par le réseau
        await tv.set("on", False)
        await until(lambda: key_packet("KEY_POWEROFF") in received)
        self.assertTrue(received.startswith(auth_packet()))
        server.close()
        clients[0].close()  # la TV s'éteint : elle coupe la connexion
        await until(lambda: tv.on is False)
        self.assertEqual(tv.state(), {"connected": False, "on": False})
        await tv.stop()
