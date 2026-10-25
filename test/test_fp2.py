"""Tests hors ligne (aucun matériel) : python -m unittest discover -s test -p "test_*.py" -v"""

import asyncio
import stat
import os
import sys
import tempfile
import tomllib
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# configuration de test (test/config/), jamais celle de l'installation
os.environ["HABCTRL_CONFIG"] = str(Path(__file__).resolve().parent / "config")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohomekit.exceptions import AccessoryDisconnectedError, AccessoryNotFoundError  # noqa: E402
from aiohomekit.zeroconf import HomeKitService  # noqa: E402

import drivers.homekit as homekit  # noqa: E402
from core import ROOT, devices as core_devices, pair as core_pair  # noqa: E402
from drivers.homekit import fp2, pair as homekit_pair  # noqa: E402

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
    device = fp2.FP2(**kwargs)
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
            mock.patch.object(fp2, "open_session", fake_controller),
            mock.patch.object(fp2, "connect", fake_connect),
            mock.patch.object(fp2, "links", lambda: {"fp2": "HomeKit"}),
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
            await driver.set("on", True)

    async def test_stop_after_start_unreachable_pairing_missing(self):
        # sans appairage enregistré : aucune connexion tentée (signalé par equipements.txt)
        driver = make_fp2("inconnu_test")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(homekit, "PAIRING_DIR", Path(tmp)),
        ):
            await driver.start()
        self.assertFalse(driver._tasks)
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
            mock.patch.object(fp2, "open_session", fake_controller),
            mock.patch.object(fp2, "connect", fake_connect),
            mock.patch.object(fp2, "links", lambda: {"fp2": "HomeKit"}),
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
        saved = tomllib.loads(path.read_text())
        self.assertEqual(saved["hap"]["AccessoryPairingID"], "AA:BB:CC:DD:EE:FF")
        self.assertFalse(set(saved["hap"]) & set(homekit.ENDPOINT_KEYS))
        self.assertEqual(homekit.load_pairing("fp2"), saved["hap"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(pairing.closed)
        discovery.async_start_pairing.assert_awaited_once_with("fp2")

    async def test_pair_records_accessory_info(self):
        pairing = FakePairing()
        info_service = {
            "iid": 1,
            "type": f"{0x3E:08X}{BASE}",
            "characteristics": [
                char(2, 0x20, "Aqara", ["pr"]),
                char(3, 0x21, "PS-S02D", ["pr"]),
                char(4, 0x30, "SN123", ["pr"]),
                char(5, 0x52, "1.2.3", ["pr"]),
                char(6, 0x23, "Presence-Sensor-FP2", ["pr"]),
            ],
        }
        pairing.accessories[0]["services"].insert(0, info_service)

        async def finish(_pin):
            return pairing

        discovery = SimpleNamespace(async_start_pairing=mock.AsyncMock(return_value=finish))
        path = await homekit.pair_accessory(discovery, "fp2", "123-45-678")
        saved = tomllib.loads(path.read_text())
        self.assertEqual(
            {k: saved[k] for k in ("manufacturer", "model", "serial", "firmware", "name")},
            {
                "manufacturer": "Aqara",
                "model": "PS-S02D",
                "serial": "SN123",
                "firmware": "1.2.3",
                "name": "Presence-Sensor-FP2",
            },
        )
        self.assertIn("paired", saved)
        self.assertNotIn("hardware", saved)  # absent de l'accessoire : pas de ligne vide

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
        self.file = self.path / "devices.py"
        self.existing = (
            "from drivers.yeelight.bulb import YeelightBulb\n\n"
            'salon = YeelightBulb(host="192.0.2.10")\n'
        )
        self.file.write_text(self.existing)

    def test_appended_with_import(self):
        for dev_id in ("fp2", "capteur"):
            core_devices.add_device(
                dev_id, "homekit.fp2", "HomeKit : X", path=self.path
            )
        self.assertEqual(
            self.file.read_text(encoding="utf-8"),
            "from drivers.yeelight.bulb import YeelightBulb\n"
            "from drivers.homekit.fp2 import FP2\n\n"
            'salon = YeelightBulb(host="192.0.2.10")\n\n'
            "# HomeKit : X\n"
            'fp2 = FP2(zonemapping={0: "Global"})\n\n'
            "# HomeKit : X\n"
            'capteur = FP2(zonemapping={0: "Global"})\n',
        )

    def test_generated_file_loads_as_devices(self):
        package = self.path.parent / "devs_generated"
        core_devices.add_device("capteur", "homekit.fp2", path=package)
        core_devices.add_device("prise", "zigbee.plug", path=package)
        sys.path.insert(0, str(package.parent))
        self.addCleanup(sys.path.remove, str(package.parent))
        devices = core_devices.load_devices("devs_generated")
        self.assertEqual(set(devices), {"capteur", "prise"})
        self.assertEqual(devices["capteur"]._aliases, {0: "Global"})

    def test_refuses_bad_id_unknown_driver(self):
        for dev_id, driver in (
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
        self.assertEqual(self.file.read_text(), self.existing)


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

    def test_every_device_variable_of_every_file(self):
        self.write(
            "salon",
            "from drivers.homekit.fp2 import FP2\n"
            "from drivers.zigbee.plug import Plug\n"
            "HOTE = '192.0.2.1'\n"
            "fp2 = FP2(zonemapping={1: 'salon'})\n"
            "prise = Plug()\n",
        )
        self.write("groupe", f"from {self.package}.salon import prise\n")  # importé : compté une fois
        self.write("_ignore", "raise RuntimeError\n")
        devices = core_devices.load_devices(self.package)
        self.assertEqual(set(devices), {"fp2", "prise"})
        self.assertEqual((devices["fp2"].id, devices["prise"].id), ("fp2", "prise"))
        self.assertEqual(devices["fp2"]._aliases, {1: "salon"})
        self.assertEqual(devices["fp2"].state(), {"connected": False})

    def test_same_id_in_two_files_is_signalled(self):
        plug = "from drivers.zigbee.plug import Plug\nlampe = Plug()\n"
        self.write("cuisine", plug)
        self.write("salon", plug)
        with self.assertLogs("core.devices", "WARNING") as logs:
            devices = core_devices.load_devices(self.package)
        self.assertEqual(
            logs.output, ["WARNING:core.devices:lampe : déclaré deux fois (cuisine.py, salon.py)"]
        )
        self.assertEqual(set(devices), {"lampe"})

    def test_config_devices_load(self):
        devices = core_devices.load_devices()
        self.assertEqual(set(devices), {"salon", "cuisine", "fp2", "prise_salon"})
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
        with self.assertLogs("core.devices", "WARNING") as logs:
            self.assertEqual(core_devices.driver_for_model("X1", self.path), "")  # pilote absent
        self.assertIn("inutilisable", logs.output[0])

    def test_missing_file(self):
        self.assertEqual(core_devices.driver_for_model("PS-S02D", self.path), "")

    def test_repo_mapping_points_to_existing_driver(self):
        models = tomllib.loads((ROOT / "drivers" / "homekit" / "models.toml").read_text())
        table = homekit_pair.Pairing(ScriptedDialog()).models
        self.assertEqual(core_devices.driver_for_model("PS-S02D", table), "homekit.fp2")
        for driver in models["models"].values():
            core_devices.load_driver(driver)

    def test_zigbee_mapping_points_to_existing_driver(self):
        models = tomllib.loads((ROOT / "drivers" / "zigbee" / "models.toml").read_text())
        for driver in models["models"].values():
            core_devices.load_driver(driver)


class ScriptedDialog:
    """Dialogue de l'appairage sans page : réponses données d'avance, messages gardés."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.out: list[str] = []

    def say(self, text: str) -> None:
        self.out.append(text)

    def wait(self, seconds: int) -> None:
        pass

    async def ask(self, prompt: str, choices=None, secret: bool = False) -> str:
        self.out.append(prompt)
        return self.answers.pop(0)


class PairRunTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.devices = Path(tmp.name) / "devices"
        self.declared = {}  # équipements déclarés dans devices/
        for target, name, value in (
            (core_devices, "DEVICES_DIR", self.devices),
            (core_pair, "load_devices", lambda: self.declared),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(homekit, "PAIRING_DIR", Path(tmp.name) / "homekit")
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

    async def run_pairing(self, discovery, models="PS-S02D", answer="fp2"):
        @asynccontextmanager
        async def fake_controller():
            yield object()

        known = {"PS-S02D": "homekit.fp2"} if models else {}
        with (
            mock.patch.object(homekit, "open_session", fake_controller),
            mock.patch.object(homekit_pair, "discover", mock.AsyncMock(return_value=[discovery])),
            mock.patch.object(core_pair, "driver_for_model", lambda m, _path: known.get(m[0], "")),
        ):
            dialog = ScriptedDialog(answer, "123-45-678")  # vide : garder l'identifiant reconnu
            ok = await homekit_pair.Pairing(dialog).run(0)
        return ok, "\n".join(dialog.out)

    async def test_known_model_fills_driver(self):
        ok, out = await self.run_pairing(self.discovery())
        self.assertTrue(ok)
        config_path = self.devices / "devices.py"
        text = config_path.read_text(encoding="utf-8")
        self.assertIn("from drivers.homekit.fp2 import FP2", text)
        self.assertIn('fp2 = FP2(zonemapping={0: "Global"})', text)
        self.assertIn("modèle PS-S02D", text)
        self.assertTrue(homekit.pairing_file("fp2").exists())
        self.assertNotIn("Modèle inconnu", out)
        # second appel (accessoire réinitialisé) : reconnu, Entrée refait le lien
        self.declared["fp2"] = fp2.FP2()
        ok, out = await self.run_pairing(self.discovery(), answer="")
        self.assertTrue(ok)
        self.assertIn("« fp2 » déjà déclaré dans devices/, inchangé", out)
        self.assertEqual(config_path.read_text(encoding="utf-8"), text)

    async def test_unknown_model_declares_nothing(self):
        ok, out = await self.run_pairing(self.discovery(model="Z9"))
        self.assertTrue(ok)
        self.assertFalse((self.devices / "devices.py").exists())
        self.assertIn("Modèle inconnu", out)
        self.assertTrue(homekit.pairing_file("fp2").exists())

    async def test_discovery_shows_every_field(self):
        _code, out = await self.run_pairing(self.discovery())
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

    async def test_already_paired_accessory(self):
        ok, _out = await self.run_pairing(self.discovery(paired=True))
        self.assertFalse(ok)
        self.assertFalse((self.devices / "fp2.toml").exists())
        self.assertFalse(homekit.pairing_file("fp2").exists())


if __name__ == "__main__":
    unittest.main()
