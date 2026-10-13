"""Tests hors ligne (aucun matériel) : python -m unittest discover -s test -p "test_*.py" -v"""

import asyncio
import contextlib
import io
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

from aiohomekit.exceptions import AccessoryDisconnectedError, AccessoryNotFoundError  # noqa: E402
from aiohomekit.zeroconf import HomeKitService  # noqa: E402

import drivers.homekit as homekit  # noqa: E402
from core import ROOT, devices as core_devices  # noqa: E402
from drivers.homekit import fp2, pair as pair_cli  # noqa: E402

BASE = "-0000-1000-8000-0026BB765291"


def char(iid, short, value, perms):
    return {"iid": iid, "type": f"{short:08X}{BASE}", "value": value, "perms": perms}


INDEX_TYPE = "C8622A33-826A-4DD3-9BE9-D496361F29BB"


def presence_key(index):
    return (1, 102 + 4 * index)  # (aid, iid) de la présence du capteur « index »


def accessories(indexes=(0, 1)):
    """Structure du vrai FP2 : un service « Presence Sensor N » par capteur, + luminosité."""
    services = [
        {
            "iid": 100 + 4 * i,
            "characteristics": [
                char(101 + 4 * i, 0x23, f"Presence Sensor {i + 1}", ["pr"]),
                char(102 + 4 * i, 0x71, 0, ["pr", "ev"]),
                {
                    "iid": 103 + 4 * i,
                    "type": INDEX_TYPE,
                    "value": i,
                    "perms": ["pr", "hd"],
                },
            ],
        }
        for i in indexes
    ]
    services.append(
        {
            "iid": 30,
            "characteristics": [
                char(31, 0x23, "Light Sensor", ["pr"]),
                char(32, 0x6B, 42.5, ["pr", "ev"]),
            ],
        }
    )
    return [{"aid": 1, "services": services}]


def make_fp2(dev_id="fp2", **kwargs):
    device = fp2.FP2(name="FP2", **kwargs)
    device.configure(dev_id)  # normalement fait par load_devices()
    return device


class FakePairing:
    def __init__(self):
        self.accessories = accessories()
        self.values = {presence_key(0): 0, presence_key(1): 1, (1, 32): 42.5}
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
        self.read_error: Exception | None = None
        self.listeners = {"event": [], "availability": [], "config": []}

    async def list_accessories_and_characteristics(self):
        return self.accessories

    async def get_characteristics(self, keys):
        if self.read_error:
            raise self.read_error
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
    def test_one_attribute_per_sensor_index(self):
        mapping = fp2.build_mapping(accessories((0, 1)), {0: "Global"})
        self.assertEqual(
            {key: entity.attr for key, entity in mapping.entities.items()},
            {
                presence_key(0): "zone.Global",
                presence_key(1): "zone.1",
                (1, 32): "illuminance",
            },
        )
        self.assertEqual(mapping.event_keys, {presence_key(0), presence_key(1), (1, 32)})

    def test_aliases_are_scoped_to_zone(self):
        mapping = fp2.build_mapping(
            accessories((0, 1, 2)), {0: "Global", 1: "connected", 2: "zone2"}
        )
        self.assertEqual(
            {entity.attr for entity in mapping.entities.values()},
            {"zone.Global", "zone.connected", "zone.zone2", "illuminance"},
        )

    def test_attribute_follows_index_not_position(self):
        data = accessories((5, 3))
        mapping = fp2.build_mapping(data)
        self.assertEqual(mapping.entities[presence_key(5)].attr, "zone.5")
        self.assertEqual(mapping.entities[presence_key(3)].attr, "zone.3")

    def test_without_index_falls_back_to_ids(self):
        data = accessories((0,))
        data[0]["services"][0]["characteristics"].pop()  # retire « Sensor Index »
        mapping = fp2.build_mapping(data)
        self.assertEqual(mapping.entities[presence_key(0)].attr, "zone.1_102")

    def test_duplicate_index_does_not_break_mapping(self):
        data = accessories((0, 1))
        data[0]["services"][1]["characteristics"][2]["value"] = 0
        mapping = fp2.build_mapping(data, {0: "Global"})
        self.assertEqual(mapping.entities[presence_key(0)].attr, "zone.Global")
        self.assertEqual(mapping.entities[presence_key(1)].attr, "zone.Global")

    def test_events_only(self):
        data = accessories((0,))
        data[0]["services"][0]["characteristics"][1]["perms"] = ["pr"]  # pas d'événements
        self.assertNotIn(presence_key(0), fp2.build_mapping(data).entities)


class DriverTest(unittest.IsolatedAsyncioTestCase):
    async def start(self, pairing, **zones):
        driver = make_fp2(**zones)
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
            {"connected": True, "zone.0": False, "zone.1": True, "illuminance": 42.5},
        )
        self.assertEqual(pairing.subscribed, {presence_key(0), presence_key(1), (1, 32)})
        self.assertEqual(len(self.states), 1)

    async def test_events_notify_only_real_changes(self):
        pairing = FakePairing()
        driver = await self.start(pairing)
        pairing.emit("event", {presence_key(0): {"value": 1}})
        self.assertTrue(driver.state()["zone.0"])
        pairing.emit("event", {presence_key(0): {"value": 1}})  # même valeur
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
        self.assertEqual(driver.state(), {"connected": False})
        pairing.values[presence_key(0)] = 1  # changé pendant la coupure
        pairing.emit("availability", True)
        await until(lambda: driver.state().get("connected"))
        self.assertTrue(driver.state()["zone.0"])
        self.assertEqual([s["connected"] for s in self.states], [True, False, True])

    async def test_config_change_rebuilds_mapping(self):
        pairing = FakePairing()
        driver = await self.start(pairing)
        pairing.accessories = accessories((0, 1, 2))  # un capteur ajouté dans l'appli Aqara
        pairing.values[presence_key(2)] = 1
        pairing.emit("config", 2)
        await until(lambda: "zone.2" in driver.state())
        self.assertTrue(driver.state()["zone.2"])
        self.assertIn(presence_key(2), pairing.subscribed)
        pairing.accessories = accessories((0,))  # puis deux retirés
        pairing.emit("config", 3)
        await until(lambda: "zone.1" not in driver.state())
        self.assertNotIn("zone.2", driver.state())
        self.assertEqual(pairing.subscribed, {presence_key(0), (1, 32)})

    async def test_aliases_from_config(self):
        driver = await self.start(
            FakePairing(),
            zonemapping={"0": "Global", "1": "salon", "5": "inexistante", "toto": "ignored"},
        )
        self.assertEqual(
            driver.state(),
            {
                "connected": True,
                "zone.Global": False,
                "zone.salon": True,
                "illuminance": 42.5,
            },
        )

    async def test_failed_full_resync_keeps_previous_state(self):
        pairing = FakePairing()
        driver = await self.start(pairing)
        before = driver.state()
        pairing.accessories = accessories((0, 1, 2))
        pairing.read_error = AccessoryDisconnectedError("coupé")
        with self.assertLogs("drivers.homekit.fp2", "WARNING"):
            pairing.emit("config", 2)
            await until(lambda: not driver._tasks)
        self.assertEqual(driver.state(), before)  # ni valeurs vidées, ni mapping à moitié changé
        pairing.read_error = None
        pairing.values[presence_key(2)] = 1
        pairing.emit("config", 3)
        await until(lambda: "zone.2" in driver.state())
        self.assertIn(presence_key(2), pairing.subscribed)

    async def test_new_session_resubscribes_after_failed_first_sync(self):
        first, second = FakePairing(), FakePairing()
        first.read_error = AccessoryDisconnectedError("coupé")
        pairings = [first, second]

        async def fake_connect(_controller, _dev_id, _wait):
            return pairings.pop(0)

        @asynccontextmanager
        async def fake_controller():
            yield object()

        driver = make_fp2()
        with (
            mock.patch.object(fp2, "RETRY_MIN", 0.01),
            mock.patch.object(fp2, "open_controller", fake_controller),
            mock.patch.object(fp2, "connect", fake_connect),
            self.assertLogs("drivers.homekit.fp2", "WARNING"),
        ):
            driver._backoff = 0.01
            await driver.start()
            await until(lambda: driver.state().get("connected"))
            second.emit("event", {presence_key(0): {"value": 1}})
        await driver.stop()
        self.assertEqual(second.subscribed, {presence_key(0), presence_key(1), (1, 32)})
        self.assertTrue(driver.state().get("zone.0"))

    async def test_set_refused(self):
        driver = make_fp2()
        with self.assertRaises(ValueError):
            await driver.set("power", "on")

    async def test_stop_after_start_unreachable_pairing_missing(self):
        # sans appairage enregistré : erreur claire journalisée, pas de boucle infinie
        driver = make_fp2("inconnu_test")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(homekit, "PAIRING_DIR", Path(tmp)),
        ):
            with self.assertLogs("drivers.homekit.fp2", "ERROR"):
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

        driver = make_fp2()
        with (
            mock.patch.object(fp2, "RETRY_MIN", 0.01),
            mock.patch.object(fp2, "open_controller", fake_controller),
            mock.patch.object(fp2, "connect", fake_connect),
            self.assertLogs("drivers.homekit.fp2", "WARNING"),
        ):
            driver._backoff = 0.01
            await driver.start()
            await until(lambda: driver.state().get("connected"))
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
        path = await homekit.pair_accessory(discovery, "fp2", "123-45-678")
        saved = json.loads(path.read_text())
        self.assertEqual(saved["AccessoryPairingID"], "AA:BB:CC:DD:EE:FF")
        self.assertFalse(set(saved) & set(homekit.ENDPOINT_KEYS))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(pairing.closed)
        discovery.async_start_pairing.assert_awaited_once_with("fp2")

    async def test_pair_refuses_bad_pin(self):
        with self.assertRaises(ValueError):
            await homekit.pair_accessory(SimpleNamespace(), "fp2", "12345678")

    async def test_connect_takes_address_from_mdns(self):
        homekit.save_pairing(
            "fp2", {"AccessoryPairingID": "AA:BB:CC:DD:EE:FF", "iOSDeviceLTSK": "sk"}
        )
        description = HomeKitService(
            name="FP2",
            id="AA:BB:CC:DD:EE:FF",
            model="PS-S02D",
            feature_flags=0,
            status_flags=0,
            config_num=1,
            state_num=1,
            category=2,
            protocol_version="1.1",
            type="_hap._tcp.local.",
            address="192.168.1.50",
            addresses=["192.168.1.50"],
            port=5555,
        )
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
        self.path = Path(tmp.name) / "devices"
        self.path.mkdir()
        (self.path / "salon.py").write_text("salon = None\n")

    def test_add_with_driver_and_name(self):
        core_devices.add_device("fp2", "homekit.fp2", 'Présence "salon"', "HomeKit : X", path=self.path)
        text = (self.path / "fp2.py").read_text(encoding="utf-8")
        self.assertEqual(
            text,
            "# HomeKit : X\n"
            "from drivers.homekit.fp2 import FP2\n\n"
            'fp2 = FP2(name="Présence \\"salon\\"", zonemapping={0: "Global"})\n',
        )
        self.assertEqual((self.path / "salon.py").read_text(), "salon = None\n")

    def test_generated_file_loads_as_a_device(self):
        package = self.path.parent / "devs_generated"
        core_devices.add_device("capteur", "homekit.fp2", path=package)
        sys.path.insert(0, str(package.parent))
        self.addCleanup(sys.path.remove, str(package.parent))
        devices = core_devices.load_devices("devs_generated")
        self.assertEqual(set(devices), {"capteur"})
        self.assertEqual(devices["capteur"]._aliases, {0: "Global"})

    def test_refuses_duplicates_bad_id_unknown_driver(self):
        for dev_id, driver in (
            ("salon", "fp2"),
            ("a b", "fp2"),
            ("a-b", "fp2"),
            ("1abc", "fp2"),
            ("_x", "fp2"),
            ("class", "fp2"),
            ("../x", "fp2"),
            ("neuf", "nexistepas"),
            ("neuf", ""),
        ):
            with self.subTest(dev_id=dev_id, driver=driver), self.assertRaises(ValueError):
                core_devices.add_device(dev_id, driver, path=self.path)
        self.assertEqual([f.name for f in self.path.iterdir()], ["salon.py"])


class LoadDevicesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.package = f"devs_{id(self)}"
        self.path = Path(tmp.name) / self.package
        self.path.mkdir()
        sys.path.insert(0, tmp.name)
        self.addCleanup(sys.path.remove, tmp.name)

    def write(self, name, text):
        (self.path / f"{name}.py").write_text(text, encoding="utf-8")

    def test_id_from_file_name_and_default_name(self):
        self.write("fp2", "from drivers.homekit.fp2 import FP2\nfp2 = FP2(zonemapping={1: 'salon'})\n")
        self.write("_ignore", "raise RuntimeError\n")
        devices = core_devices.load_devices(self.package)
        self.assertEqual(set(devices), {"fp2"})
        self.assertEqual((devices["fp2"].id, devices["fp2"].name), ("fp2", "fp2"))
        self.assertEqual(devices["fp2"]._aliases, {1: "salon"})
        self.assertEqual(devices["fp2"].state(), {"connected": False})

    def test_variable_missing_or_wrong_type(self):
        self.write("salon", "x = 1\n")
        with self.assertRaises(ValueError):
            core_devices.load_devices(self.package)

    def test_repo_devices_load(self):
        devices = core_devices.load_devices()
        self.assertEqual(set(devices), {"salon", "cuisine", "fp2", "prise_salon"})
        self.assertEqual(devices["salon"].name, "Salon")
        self.assertEqual(devices["fp2"]._aliases, {0: "Global", 1: "salon", 2: "cuisine"})


def service(model="PS-S02D"):
    return HomeKitService(
        name="FP2-1234",
        id="AA:BB:CC:DD:EE:FF",
        model=model,
        feature_flags=0,
        status_flags=0,
        config_num=7,
        state_num=1,
        category=2,
        protocol_version="1.1",
        type="_hap._tcp.local.",
        address="192.168.1.50",
        addresses=["192.168.1.50"],
        port=5555,
    )


class DriverForModelTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "models.toml"

    def test_lookup(self):
        self.path.write_text('[models]\n"PS-S02D" = "homekit.fp2"\n"X1" = "nexistepas"\n')
        self.assertEqual(core_devices.driver_for_model("PS-S02D", self.path), "homekit.fp2")
        self.assertEqual(core_devices.driver_for_model("inconnu", self.path), "")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(core_devices.driver_for_model("X1", self.path), "")  # pilote absent
        self.assertIn("inutilisable", out.getvalue())

    def test_missing_file(self):
        self.assertEqual(core_devices.driver_for_model("PS-S02D", self.path), "")

    def test_repo_mapping_points_to_existing_driver(self):
        models = tomllib.loads((ROOT / "drivers" / "homekit" / "models.toml").read_text())
        self.assertEqual(core_devices.driver_for_model("PS-S02D", pair_cli.MODELS), "homekit.fp2")
        for driver in models["models"].values():
            core_devices.load_driver(driver)

    def test_zigbee_mapping_points_to_existing_driver(self):
        models = tomllib.loads((ROOT / "drivers" / "zigbee" / "models.toml").read_text())
        for driver in models["models"].values():
            core_devices.load_driver(driver)


class PairCliTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.devices = Path(tmp.name) / "devices"
        self.devices.mkdir()
        (self.devices / "salon.py").write_text("salon = None\n")
        patcher = mock.patch.object(homekit, "PAIRING_DIR", Path(tmp.name) / "homekit")
        patcher.start()
        self.addCleanup(patcher.stop)
        # valeurs par défaut de chemin figées à la définition : on les redirige explicitement
        for func, defaults in (
            (core_devices.check_new_device, ("", self.devices)),
            (core_devices.add_device, ("", "", self.devices, None)),
        ):
            patcher = mock.patch.object(func, "__defaults__", defaults)
            patcher.start()
            self.addCleanup(patcher.stop)

    def discovery(self, model="PS-S02D", paired=False):
        async def finish(_pin):
            return FakePairing()

        return SimpleNamespace(
            description=service(model),
            paired=paired,
            async_start_pairing=mock.AsyncMock(return_value=finish),
        )

    async def run_cli(self, discovery, models="PS-S02D"):
        @asynccontextmanager
        async def fake_controller():
            yield object()

        args = SimpleNamespace(id="fp2", timeout=0)
        known = {"PS-S02D": "homekit.fp2"} if models else {}
        with (
            mock.patch.object(pair_cli, "open_controller", fake_controller),
            mock.patch.object(pair_cli, "discover", mock.AsyncMock(return_value=[discovery])),
            mock.patch.object(pair_cli, "driver_for_model", lambda m, _path: known.get(m, "")),
            mock.patch("builtins.input", return_value="1"),
            mock.patch("getpass.getpass", return_value="123-45-678"),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            code = await pair_cli.run(args)
        return code, out.getvalue()

    async def test_known_model_fills_driver(self):
        code, out = await self.run_cli(self.discovery())
        self.assertEqual(code, 0)
        config_path = self.devices / "fp2.py"
        text = config_path.read_text(encoding="utf-8")
        self.assertIn("from drivers.homekit.fp2 import FP2", text)
        self.assertIn('fp2 = FP2(zonemapping={0: "Global"})', text)
        self.assertIn("modèle PS-S02D", text)
        self.assertTrue(homekit.pairing_file("fp2").exists())
        self.assertNotIn("Modèle inconnu", out)
        # second appel : refusé, rien n'est modifié
        self.assertEqual((await self.run_cli(self.discovery()))[0], 1)

    async def test_unknown_model_creates_no_device_file(self):
        code, out = await self.run_cli(self.discovery(model="Z9"))
        self.assertEqual(code, 0)
        self.assertFalse((self.devices / "fp2.py").exists())
        self.assertIn("Modèle inconnu", out)
        self.assertTrue(homekit.pairing_file("fp2").exists())

    async def test_discovery_shows_every_field(self):
        _code, out = await self.run_cli(self.discovery())
        for name in (
            "name",
            "id",
            "model",
            "feature_flags",
            "status_flags",
            "config_num",
            "state_num",
            "category",
            "protocol_version",
            "type",
            "address",
            "addresses",
            "port",
        ):
            self.assertIn(f"{name} :", out)
        self.assertIn("non appairé", out)

    async def test_already_paired_accessory(self):
        code, _out = await self.run_cli(self.discovery(paired=True))
        self.assertEqual(code, 1)
        self.assertFalse((self.devices / "fp2.toml").exists())
        self.assertFalse(homekit.pairing_file("fp2").exists())


if __name__ == "__main__":
    unittest.main()
