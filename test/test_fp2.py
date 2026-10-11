"""Tests hors ligne (aucun matériel) : python -m unittest discover -s test -p "test_*.py" -v"""

import asyncio
import json
import stat
import sys
import tempfile
import tomllib
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohomekit.exceptions import AccessoryNotFoundError  # noqa: E402

import pair as pair_cli  # noqa: E402
from core import devices as core_devices  # noqa: E402
from core import homekit  # noqa: E402
from drivers import fp2  # noqa: E402

BASE = "-0000-1000-8000-0026BB765291"


def char(iid, short, value, perms):
    return {"iid": iid, "type": f"{short:08X}{BASE}", "value": value, "perms": perms}


def accessories(zone="Bureau"):
    return [
        {
            "aid": 1,
            "services": [
                {
                    "iid": 10,
                    "characteristics": [
                        char(11, 0x23, "Global Presence", ["pr"]),
                        char(12, 0x71, 0, ["pr", "ev"]),
                    ],
                },
                {
                    "iid": 20,
                    "characteristics": [
                        char(21, 0x23, zone, ["pr"]),
                        char(22, 0x71, 1, ["pr", "ev"]),
                    ],
                },
                {
                    "iid": 30,
                    "characteristics": [
                        char(31, 0x23, "Luminosity", ["pr"]),
                        char(32, 0x6B, 42.5, ["pr", "ev"]),
                    ],
                },
            ],
        }
    ]


class FakePairing:
    def __init__(self, zone="Bureau"):
        self.accessories = accessories(zone)
        self.values = {(1, 12): 0, (1, 22): 1, (1, 32): 42.5}
        self.is_available = True
        self.subscribed: set = set()
        self.closed = False
        self.pairing_data = {
            "AccessoryPairingID": "AA:BB:CC:DD:EE:FF",
            "AccessoryLTPK": "ltpk",
            "iOSPairingId": "ios",
            "iOSDeviceLTPK": "pk",
            "iOSDeviceLTSK": "sk",
            "AccessoryIP": "10.0.0.9",
            "AccessoryIPs": ["10.0.0.9"],
            "AccessoryPort": 1234,
            "Connection": "IP",
        }
        self.listeners = {"event": [], "availability": [], "config": []}

    async def list_accessories_and_characteristics(self):
        return self.accessories

    async def get_characteristics(self, keys):
        return {key: {"value": self.values[key]} for key in keys}

    async def subscribe(self, keys):
        self.subscribed |= set(keys)
        return {}

    async def unsubscribe(self, keys):
        self.subscribed -= set(keys)

    async def close(self):
        self.closed = True

    def _listen(self, kind, callback):
        self.listeners[kind].append(callback)
        return lambda: self.listeners[kind].remove(callback)

    def dispatcher_connect(self, callback):
        return self._listen("event", callback)

    def dispatcher_availability_changed(self, callback):
        return self._listen("availability", callback)

    def dispatcher_connect_config_changed(self, callback):
        return self._listen("config", callback)

    def emit(self, kind, arg):
        for callback in list(self.listeners[kind]):
            callback(arg)


async def until(condition, limit=2.0):
    async with asyncio.timeout(limit):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.005)  # noqa: ASYNC110


class MappingTest(unittest.TestCase):
    def test_global_zone_light(self):
        mapping = fp2.build_mapping(accessories())
        self.assertEqual(
            {key: entity.attr for key, entity in mapping.entities.items()},
            {(1, 12): "presence", (1, 22): "bureau", (1, 32): "illuminance"},
        )
        self.assertEqual(mapping.event_keys, {(1, 12), (1, 22), (1, 32)})
        self.assertEqual(mapping.warnings, [])

    def test_zone_name_collision_and_accents(self):
        mapping = fp2.build_mapping(accessories(zone="Illuminance"))
        self.assertEqual(mapping.entities[(1, 22)].attr, "illuminance_1_22")
        mapping = fp2.build_mapping(accessories(zone="Salle à manger"))
        self.assertEqual(mapping.entities[(1, 22)].attr, "salle_a_manger")

    def test_ambiguous_global(self):
        data = accessories(zone="Salon")
        data[0]["services"][0]["characteristics"][0]["value"] = "Zone 1"
        mapping = fp2.build_mapping(data)
        self.assertNotIn("presence", {e.attr for e in mapping.entities.values()})
        self.assertTrue(mapping.warnings)


class DriverTest(unittest.IsolatedAsyncioTestCase):
    async def start(self, pairing):
        driver = fp2.FP2(id="fp2", name="FP2")
        self.states: list[dict] = []
        driver.subscribe(self.states.append)
        task = asyncio.create_task(driver._serve(pairing))
        self.addAsyncCleanup(self.stop, task)
        await until(lambda: driver._pairing is not None)
        return driver

    @staticmethod
    async def stop(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_initial_state_and_subscription(self):
        pairing = FakePairing()
        driver = await self.start(pairing)
        self.assertEqual(
            driver.state(),
            {"reachable": True, "presence": False, "bureau": True, "illuminance": 42.5},
        )
        self.assertEqual(pairing.subscribed, {(1, 12), (1, 22), (1, 32)})
        self.assertEqual(len(self.states), 1)

    async def test_events_notify_only_real_changes(self):
        pairing = FakePairing()
        driver = await self.start(pairing)
        pairing.emit("event", {(1, 12): {"value": 1}})
        self.assertTrue(driver.state()["presence"])
        pairing.emit("event", {(1, 12): {"value": 1}})  # même valeur
        pairing.emit("event", {})  # reconnexion : rien à dire
        pairing.emit("event", {(9, 9): {"value": 1}})  # caractéristique inconnue
        self.assertEqual(len(self.states), 2)

    async def test_bad_value_is_ignored(self):
        pairing = FakePairing()
        driver = await self.start(pairing)
        pairing.emit("event", {(1, 32): {"value": None}})
        self.assertEqual(driver.state()["illuminance"], 42.5)

    async def test_disconnect_then_resync(self):
        pairing = FakePairing()
        driver = await self.start(pairing)
        pairing.emit("availability", False)
        self.assertEqual(driver.state(), {"reachable": False})
        pairing.values[(1, 12)] = 1  # changé pendant la coupure
        pairing.emit("availability", True)
        await until(lambda: driver.state().get("reachable"))
        self.assertTrue(driver.state()["presence"])
        self.assertEqual([s["reachable"] for s in self.states], [True, False, True])

    async def test_config_change_rebuilds_mapping(self):
        pairing = FakePairing()
        driver = await self.start(pairing)
        pairing.accessories = accessories(zone="Cuisine")
        pairing.emit("config", 2)
        await until(lambda: "cuisine" in driver.state())
        self.assertNotIn("bureau", driver.state())

    async def test_set_refused(self):
        driver = fp2.FP2(id="fp2", name="FP2")
        with self.assertRaises(ValueError):
            await driver.set("power", "on")

    async def test_stop_after_start_unreachable_pairing_missing(self):
        # sans appairage enregistré : erreur claire journalisée, pas de boucle infinie
        driver = fp2.FP2(id="inconnu_test", name="FP2")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(homekit, "PAIRING_DIR", Path(tmp)),
        ):
            with self.assertLogs("drivers.fp2", "ERROR"):
                await driver.start()
                await until(lambda: not driver._tasks)
        await driver.stop()

    async def test_retries_until_accessory_found(self):
        pairing = FakePairing()
        attempts = []

        async def fake_connect(_controller, dev_id, _timeout):
            attempts.append(dev_id)
            if len(attempts) < 3:
                raise AccessoryNotFoundError("absent")
            return pairing

        @asynccontextmanager
        async def fake_controller():
            yield object()

        driver = fp2.FP2(id="fp2", name="FP2")
        with (
            mock.patch.object(fp2, "RETRY_MIN", 0.01),
            mock.patch.object(fp2, "open_controller", fake_controller),
            mock.patch.object(fp2, "connect", fake_connect),
            self.assertLogs("drivers.fp2", "WARNING"),
        ):
            driver._backoff = 0.01
            await driver.start()
            await until(lambda: driver.state().get("reachable"))
        await driver.stop()
        self.assertEqual(len(attempts), 3)


class HomekitFilesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(homekit, "PAIRING_DIR", Path(tmp.name) / "homekit")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_pair_saves_without_ip_and_private(self):
        pairing = FakePairing()

        async def finish(pin):
            self.assertEqual(pin, "123-45-678")
            return pairing

        discovery = SimpleNamespace(async_start_pairing=mock.AsyncMock(return_value=finish))
        path = await homekit.pair(discovery, "fp2", "123-45-678")
        saved = json.loads(path.read_text())
        self.assertEqual(saved["AccessoryPairingID"], "AA:BB:CC:DD:EE:FF")
        self.assertFalse(set(saved) & set(homekit.ENDPOINT_KEYS))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(pairing.closed)
        discovery.async_start_pairing.assert_awaited_once_with("fp2")

    async def test_pair_refuses_bad_pin(self):
        with self.assertRaises(ValueError):
            await homekit.pair(SimpleNamespace(), "fp2", "12345678")

    async def test_connect_takes_address_from_mdns(self):
        homekit.save_pairing(
            "fp2", {"AccessoryPairingID": "AA:BB:CC:DD:EE:FF", "iOSDeviceLTSK": "sk"}
        )
        description = SimpleNamespace(address="192.168.1.50", addresses=["192.168.1.50"], port=5555)
        controller = SimpleNamespace(
            async_find=mock.AsyncMock(return_value=SimpleNamespace(description=description)),
            load_pairing=mock.Mock(return_value="pairing"),
        )
        self.assertEqual(await homekit.connect(controller, "fp2", 5), "pairing")
        controller.async_find.assert_awaited_once_with("AA:BB:CC:DD:EE:FF", 5)
        alias, data = controller.load_pairing.call_args.args
        self.assertEqual(alias, "fp2")
        self.assertEqual((data["AccessoryIPs"], data["AccessoryPort"]), (["192.168.1.50"], 5555))

    def test_missing_or_broken_pairing(self):
        with self.assertRaises(homekit.PairingError):
            homekit.load_pairing("absent")
        path = homekit.pairing_file("casse")
        path.parent.mkdir(parents=True)
        path.write_text("{pas du json")
        with self.assertRaises(homekit.PairingError):
            homekit.load_pairing("casse")

    def test_dev_id_cannot_escape_folder(self):
        with self.assertRaises(ValueError):
            homekit.pairing_file("../secret")


class AddDeviceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "devices.toml"
        self.path.write_text(
            '# commentaire\n\n[salon]\nname = "Salon"\ndriver = "yeelight"\nhost = "1.2.3.4"\n'
        )

    def test_add_keeps_comments_and_parses(self):
        core_devices.add_device("fp2", "fp2", 'Présence "salon"', self.path)
        text = self.path.read_text()
        self.assertTrue(text.startswith("# commentaire"))
        conf = tomllib.loads(text)
        self.assertEqual(conf["fp2"], {"name": 'Présence "salon"', "driver": "fp2"})
        self.assertEqual(conf["salon"]["host"], "1.2.3.4")

    def test_refuses_duplicates_bad_id_unknown_driver(self):
        for dev_id, driver in (
            ("salon", "fp2"),
            ("a b", "fp2"),
            ("../x", "fp2"),
            ("neuf", "nexistepas"),
        ):
            with self.subTest(dev_id=dev_id, driver=driver), self.assertRaises(ValueError):
                core_devices.add_device(dev_id, driver, "X", self.path)

    def test_load_devices_builds_fp2(self):
        core_devices.add_device("fp2", "fp2", "FP2", self.path)
        self.path.write_text(
            self.path.read_text()
            .replace('driver = "yeelight"', 'driver = "fp2"')
            .replace('host = "1.2.3.4"\n', "")
        )
        devices = core_devices.load_devices(self.path)
        self.assertIsInstance(devices["fp2"], fp2.FP2)
        self.assertEqual(devices["fp2"].state(), {"reachable": False})


class PairCliTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.devices = Path(tmp.name) / "devices.toml"
        self.devices.write_text('[salon]\nname = "Salon"\ndriver = "fp2"\n')
        for target, value in (
            (homekit, ("PAIRING_DIR", Path(tmp.name) / "homekit")),
            (core_devices, ("CONFIG_DIR", Path(tmp.name))),
        ):
            patcher = mock.patch.object(target, *value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # valeurs par défaut de chemin figées à la définition : on les redirige explicitement
        for func in (core_devices.check_new_device, core_devices.add_device):
            patcher = mock.patch.object(func, "__defaults__", (self.devices,))
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_pair_flow(self):
        pairing = FakePairing()

        async def finish(_pin):
            return pairing

        discovery = SimpleNamespace(
            description=SimpleNamespace(name="FP2-1234", id="aa:bb", model="PS-S02D"),
            paired=False,
            async_start_pairing=mock.AsyncMock(return_value=finish),
        )

        @asynccontextmanager
        async def fake_controller():
            yield object()

        args = SimpleNamespace(id="fp2", driver="fp2", name="Présence", timeout=0)
        with (
            mock.patch.object(pair_cli, "open_controller", fake_controller),
            mock.patch.object(pair_cli, "discover", mock.AsyncMock(return_value=[discovery])),
            mock.patch("builtins.input", return_value="1"),
            mock.patch("getpass.getpass", return_value="123-45-678"),
        ):
            self.assertEqual(await pair_cli.run(args), 0)
        conf = tomllib.loads(self.devices.read_text())
        self.assertEqual(conf["fp2"], {"name": "Présence", "driver": "fp2"})
        self.assertTrue(homekit.pairing_file("fp2").exists())
        # second appel : refusé, rien n'est modifié
        self.assertEqual(await pair_cli.run(args), 1)

    async def test_already_paired_accessory(self):
        discovery = SimpleNamespace(
            description=SimpleNamespace(name="FP2", id="aa", model=""), paired=True
        )

        @asynccontextmanager
        async def fake_controller():
            yield object()

        args = SimpleNamespace(id="fp2", driver="fp2", name=None, timeout=0)
        with (
            mock.patch.object(pair_cli, "open_controller", fake_controller),
            mock.patch.object(pair_cli, "discover", mock.AsyncMock(return_value=[discovery])),
            mock.patch("builtins.input", return_value="1"),
        ):
            self.assertEqual(await pair_cli.run(args), 1)
        self.assertNotIn("fp2", tomllib.loads(self.devices.read_text()))
        self.assertFalse(homekit.pairing_file("fp2").exists())


if __name__ == "__main__":
    unittest.main()
