"""Tests Zigbee sans clé : instantané, appareils inutilisés, état de la prise, pair."""

import asyncio
import sqlite3
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# configuration de test (test/config/), jamais celle de l'installation
os.environ["HABCTRL_CONFIG"] = str(Path(__file__).resolve().parent / "config")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import drivers.zigbee as zigbee  # noqa: E402
from core import pair as core_pair  # noqa: E402
import drivers.zigbee.pair as zigbee_pair  # noqa: E402
import drivers.zigbee.plug as zigbee_plug  # noqa: E402
import drivers.homekit as homekit  # noqa: E402
from core.devices import id_from_name, summarize, write_summary  # noqa: E402
from core.firmware import Firmwares  # noqa: E402
from drivers.yeelight.bulb import YeelightBulb  # noqa: E402
from core import base  # noqa: E402
from drivers.zigbee.light import Light, rgb_to_xy, xy_to_rgb  # noqa: E402
from drivers.zigbee.plug import ELECTRICAL, METERING, ON_OFF, Plug  # noqa: E402
from drivers.zigbee.button import Button  # noqa: E402
from drivers.zigbee.sensor import Sensor  # noqa: E402
from drivers.zigbee.climate import Climate  # noqa: E402
from drivers.zigbee.contact import Contact  # noqa: E402
from drivers.zigbee.motion import Motion  # noqa: E402
from zigpy.types import EUI64  # noqa: E402
from test_fp2 import ScriptedDialog  # noqa: E402

IEEE = "a4:c1:38:d6:d7:c6:db:f6"
OTHER = "00:17:88:01:08:9d:f1:10"


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.live = Path(tmp.name) / "temp" / "zigbee.db"
        self.snapshot = Path(tmp.name) / "config" / "pairing" / "zigbee.db"
        for name, value in (("DB_FILE", self.live), ("SNAPSHOT", self.snapshot)):
            patcher = mock.patch.object(zigbee, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_save_then_restore_on_empty_temp(self):
        self.live.parent.mkdir(parents=True)
        with sqlite3.connect(self.live) as db:
            db.execute("create table devices (ieee text)")
            db.execute("insert into devices values (?)", (IEEE,))
        zigbee.save_snapshot()
        self.live.unlink()  # temp/ en RAM : vidé au redémarrage
        zigbee.restore_snapshot()
        with sqlite3.connect(self.live) as db:
            self.assertEqual(db.execute("select ieee from devices").fetchall(), [(IEEE,)])

    def test_restore_keeps_live_base(self):
        self.live.parent.mkdir(parents=True)
        self.live.write_bytes(b"vivante")
        self.snapshot.parent.mkdir(parents=True)
        self.snapshot.write_bytes(b"ancienne")
        zigbee.restore_snapshot()
        self.assertEqual(self.live.read_bytes(), b"vivante")

    def test_nothing_to_save_or_restore(self):
        zigbee.save_snapshot()
        zigbee.restore_snapshot()
        self.assertFalse(self.snapshot.exists())
        self.assertFalse(self.live.exists())


class ReportsTest(unittest.IsolatedAsyncioTestCase):
    def test_declaration_overrides_defaults(self):
        plug = Plug(IEEE, reports={"active_power": 5, "energy": 0.1, "on": None})
        self.assertEqual(plug.reports, {"active_power": 5, "energy": 0.1})
        self.assertEqual(Plug(IEEE).reports, {"on": True, "active_power": 1})

    def test_unknown_value_refused(self):
        with self.assertRaises(ValueError):
            Contact(IEEE, reports={"temperature": 1})  # type: ignore[typeddict-unknown-key]

    async def test_refused_attribute_logged(self):
        cluster = mock.Mock(cluster_id=0x0B04, bind=mock.AsyncMock())
        power, current = mock.Mock(), mock.Mock()
        power.name, current.name = "active_power", "rms_current"
        cluster.configure_reporting_multiple = mock.AsyncMock(
            return_value={
                power: zigbee.Status.SUCCESS,
                current: zigbee.Status.UNSUPPORTED_ATTRIBUTE,
            }
        )
        with self.assertLogs("drivers.zigbee", "WARNING") as logs:
            await zigbee.setup_reporting("prise", cluster, {"active_power": (5, 300, 1)})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("rms_current", logs.output[0])

    async def configure(self, device, cluster_ids, scales=None):
        """Envois spontanés demandés par _configure : {(cluster, attribut): réglage}."""
        sent = {}

        async def setup(_name, cluster, reports):
            for attr, cfg in reports.items():
                sent[cluster, attr] = cfg

        async def read(cluster, names):
            return (scales or {}).get(cluster, {})

        module = sys.modules[type(device).__module__]
        if not hasattr(module, "setup_reporting"):
            module = sys.modules["drivers.zigbee.sensor"]
        patches = [
            mock.patch.object(module, "setup_reporting", setup),
            mock.patch.object(
                module, "find_cluster", lambda _z, c: c if c in cluster_ids else None
            ),
        ]
        if hasattr(module, "read"):
            patches.append(mock.patch.object(module, "read", read))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        await device._configure(SimpleNamespace())
        return sent

    async def test_plug_changes_scaled_and_untracked_stopped(self):
        plug = Plug(IEEE, reports={"current": 0.05})
        scales = {ELECTRICAL: {"ac_current_multiplier": 1, "ac_current_divisor": 1000}}
        sent = await self.configure(plug, {ON_OFF, ELECTRICAL}, scales)
        self.assertEqual(
            sent,
            {
                (ON_OFF, "on_off"): (0, 600, 1),
                (ELECTRICAL, "active_power"): (5, 300, 1),
                (ELECTRICAL, "rms_voltage"): None,
                (ELECTRICAL, "rms_current"): (5, 300, 50),
            },
        )

    async def test_sensor_asks_its_own_values_only(self):
        sent = await self.configure(Contact(IEEE), {0x0001, 0x0006, 0x0402})
        self.assertEqual(
            sent,
            {
                (0x0001, "battery_percentage_remaining"): (3600, 43200, 2),
                (0x0402, "measured_value"): None,
            },
        )
        sent = await self.configure(Motion(IEEE), {0x0400})
        self.assertEqual(sent, {(0x0400, "measured_value"): (30, 3600, 86)})
        sent = await self.configure(Climate(IEEE, reports={"humidity": 0.2}), {0x0405})
        self.assertEqual(sent, {(0x0405, "measured_value"): (30, 3600, 20)})

    async def test_light_brightness_in_percent_and_rgb_stopped(self):
        light = Light(IEEE, reports={"brightness": 2, "rgb": None})
        clusters = {cluster_id: mock.Mock(cluster_id=cluster_id) for cluster_id in (6, 8, 0x0300)}
        clusters[0x0300].get.return_value = None  # color_capabilities non lu : tout présent
        with mock.patch(
            "drivers.zigbee.light.find_cluster", lambda _z, cluster_id: clusters[cluster_id]
        ):
            reports = {
                cluster.cluster_id: cfgs for cluster, cfgs in light._reports(SimpleNamespace())
            }
        self.assertEqual(reports[0x0008], {"current_level": (1, 600, 5)})
        self.assertEqual(
            reports[0x0300],
            {"color_temperature": (1, 600, 1), "current_x": None, "current_y": None},
        )


class PlugStateTest(unittest.TestCase):
    def make(self):
        plug = Plug(IEEE, reports={"voltage": 1, "current": 0.01, "energy": 0.01})
        plug._scales = {"active_power": 1, "voltage": 1, "current": 1 / 1000, "energy": 1 / 100}
        return plug

    def test_values_scaled_and_hidden_while_unreachable(self):
        plug = self.make()
        plug._on_attribute(ON_OFF, "on_off", True)
        plug._on_attribute(ELECTRICAL, "rms_current", 45)
        plug._on_attribute(METERING, "current_summ_delivered", 10702)
        self.assertEqual(plug.state(), {"connected": False})
        plug._connected = plug._synced = True
        self.assertEqual(
            plug.state(),
            {"connected": True, "on": True, "current": 0.045, "energy": 107.02},
        )

    def test_untracked_value_ignored(self):
        plug = Plug(IEEE)
        plug._scales = {"active_power": 1}
        plug._on_attribute(ELECTRICAL, "rms_voltage", 234)
        plug._on_attribute(ELECTRICAL, "active_power", 12)
        plug._connected = plug._synced = True
        self.assertEqual(plug.state(), {"connected": True, "active_power": 12})

    def test_on_follows_zcl_on_off(self):
        plug = self.make()
        plug._connected = plug._synced = True
        plug._on_attribute(ON_OFF, "on_off", 0)
        self.assertIs(plug.on, False)

    def test_measure_without_cluster_is_ignored(self):
        plug = self.make()
        plug._scales = {}
        plug._on_attribute(ELECTRICAL, "active_power", 12)
        plug._connected = plug._synced = True
        self.assertNotIn("active_power", plug.state())


class PlugCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_on_sends_zcl_command_and_toggle_inverts(self):
        plug = Plug(IEEE)
        plug.zdev = SimpleNamespace()
        sent = []

        async def command(cluster, name):
            sent.append(name)

        with (
            mock.patch.object(zigbee_plug, "find_cluster", return_value=object()),
            mock.patch.object(plug, "_command", command),
        ):
            await plug.set("on", True)
            await plug.set("on", "toggle")
        self.assertEqual(sent, ["on", "off"])
        self.assertIs(plug._values["on"], False)


class SensorTest(unittest.IsolatedAsyncioTestCase):
    def report(self, sensor, cluster_id, name, value):
        sensor._on_event(SimpleNamespace(cluster_id=cluster_id, attribute_name=name, value=value))

    async def test_connected_by_first_message_and_converted(self):
        sensor = Climate(IEEE)
        sensor.zdev = SimpleNamespace(skip_configuration=True)
        self.assertEqual(sensor.state(), {"connected": False})
        self.report(sensor, 0x0402, "measured_value", 2150)
        self.report(sensor, 0x0001, "battery_percentage_remaining", 170)
        self.report(sensor, 0x0400, "measured_value", 10001)  # d'une autre variante : ignorée
        self.report(sensor, 0x0006, "on_off", 1)
        self.assertEqual(sensor.state(), {"connected": True, "temperature": 21.5, "battery": 85})
        motion = Motion(IEEE)
        motion.zdev = SimpleNamespace(skip_configuration=True)
        self.report(motion, 0x0400, "measured_value", 10001)
        self.assertEqual(motion.illuminance, 10)

    async def test_button_relays_quirk_events(self):
        button = Button(IEEE)
        button.zdev = SimpleNamespace(skip_configuration=True)
        got = []
        button.subscribe_events(lambda name, value: got.append((name, value)))
        button.zha_send_event("double", [])
        button.zha_send_event("rotate_right", {"relative_degrees": 90})
        self.assertEqual(got, [("double", None), ("rotate_right", {"relative_degrees": 90})])
        self.assertTrue(button.connected)

    async def test_no_poll_on_attach(self):
        sensor = Sensor(IEEE)
        sensor.ieee = EUI64.convert(IEEE)
        endpoint = SimpleNamespace(in_clusters={0x0402: mock.Mock()}, out_clusters={})
        zdev = SimpleNamespace(endpoints={0: None, 1: endpoint})
        with mock.patch.object(zigbee, "ZigpyDevice", SimpleNamespace):
            sensor._attach(SimpleNamespace(devices={sensor.ieee: zdev}))
        self.assertFalse(sensor._tasks)

    async def test_ready_on_attach(self):
        """Prêt une fois rattaché, plus prêt une fois détaché : les abonnés sont prévenus."""
        sensor = Sensor(IEEE)
        zdev = SimpleNamespace(endpoints={})
        seen = []
        sensor.subscribe_ready(lambda: seen.append(sensor.ready()))
        with mock.patch.object(zigbee, "ZigpyDevice", SimpleNamespace):
            sensor._attach(SimpleNamespace(devices={sensor.ieee: zdev}))
        sensor._detach()
        self.assertEqual(seen, [False, True, False])


class LightTest(unittest.IsolatedAsyncioTestCase):
    def test_state_from_zcl(self):
        light = Light(IEEE)
        light._on_attribute(0x0006, "on_off", 1)
        light._on_attribute(0x0008, "current_level", 127)
        light._on_attribute(0x0300, "color_temperature", 370)
        light._connected = light._synced = True
        self.assertEqual(
            light.state(), {"connected": True, "on": True, "brightness": 50, "color_temp": 2703}
        )

    def test_rgb_round_trip(self):
        for rgb in ("#ff0000", "#00ff00", "#0000ff", "#ffffff"):
            self.assertEqual(xy_to_rgb(*rgb_to_xy(rgb)), rgb)

    async def test_setting_missing_feature_refused(self):
        light = Light(IEEE)
        light.zdev = SimpleNamespace()
        with mock.patch("drivers.zigbee.light.find_cluster", return_value=None):
            with self.assertRaises(ValueError):
                await light.set("rgb", "#ff0000")

    def white_light(self, caps):
        """Ampoule à clusters marche/arrêt, intensité, couleur ; color_capabilities = caps."""
        light = Light(IEEE)
        light._connected = light._synced = True
        self.sent = []
        clusters = {}
        for cluster_id in (0x0006, 0x0008, 0x0300):
            cluster = mock.Mock(cluster_id=cluster_id)
            cluster.get.return_value = caps

            def command(name, cluster_id=cluster_id):
                async def send(*args):
                    self.sent.append((cluster_id, name))
                return send

            for name in ("on", "move_to_level_with_on_off", "move_to_color_temp"):
                setattr(cluster, name, command(name))
            clusters[cluster_id] = cluster
        light.zdev = SimpleNamespace()
        patcher = mock.patch("drivers.zigbee.light.find_cluster", lambda _z, c: clusters[c])
        patcher.start()
        self.addCleanup(patcher.stop)
        return light

    async def test_undeclared_color_refused_without_sending(self):
        light = self.white_light(caps=0x10)  # température seulement
        with self.assertRaises(ValueError):
            await light.set("rgb", "#ff0000")
        self.assertEqual(self.sent, [])
        light._on_attribute(0x0300, "current_x", 20000)  # x/y envoyés quand même : ignorés
        light._on_attribute(0x0300, "current_y", 20000)
        self.assertNotIn("rgb", light.state())

    async def test_color_turns_bulb_on_first(self):
        light = self.white_light(caps=None)  # non lu : tout supposé présent
        light._raw["on_off"] = 0
        await light.set("color_temp", 2700)
        self.assertEqual(self.sent, [(0x0006, "on"), (0x0300, "move_to_color_temp")])

    async def test_set_rgb_on_white_bulb_does_what_it_can(self):
        light = self.white_light(caps=0x10)
        with self.assertLogs("core.base", "WARNING") as logs:
            light.set_rgb(30, "#ff0000")
            await asyncio.gather(*list(base.tasks))
        self.assertEqual(self.sent, [(0x0008, "move_to_level_with_on_off")])
        self.assertIn("refusée", logs.output[0])
        self.assertIs(light.on, True)


def make_zdev(ieee=IEEE, model="TS011F"):
    endpoint = SimpleNamespace(
        in_clusters={0x0006: None, 0x0000: None},
        out_clusters={},
        profile_id=0x0104,
        device_type=0x010A,
    )
    return SimpleNamespace(
        ieee=EUI64.convert(ieee),
        nwk=0x1234,
        manufacturer="Fab",
        model=model,
        endpoints={0: SimpleNamespace(), 1: endpoint},
    )


def make_app(*devices):
    """Clé simulée : le coordinateur et les appareils du réseau."""
    coordinator = SimpleNamespace(ieee=EUI64.convert("00:00:00:00:00:00:00:01"))
    return SimpleNamespace(
        devices={device.ieee: device for device in (coordinator, *devices)},
        state=SimpleNamespace(node_info=SimpleNamespace(ieee=coordinator.ieee)),
        remove=mock.AsyncMock(),
        add_listener=lambda _listener: None,
    )


class FirmwareTest(unittest.IsolatedAsyncioTestCase):
    """Firmware proposé par zigpy : liste des mises à jour, installation, échec."""

    def setUp(self):
        self.updates = mock.patch.object(zigbee, "updates", Firmwares()).start()
        self.addCleanup(mock.patch.stopall)
        self.light = Light(OTHER)
        self.light.configure("globe")

    def event(self, *versions):
        images = tuple(SimpleNamespace(version=v) for v in versions)
        return SimpleNamespace(
            images_result=SimpleNamespace(upgrades=images),
            query_cmd=SimpleNamespace(current_file_version=0x4200382A),
        )

    def test_offer_newest_then_withdraw(self):
        self.light._on_ota(self.event(0x42007530, 0x42000001))
        self.assertEqual(
            self.updates.list()["offers"],
            [{"device": "globe", "current": "0x4200382a", "version": "0x42007530"}],
        )
        self.light._on_ota(self.event())
        self.assertEqual(self.updates.list()["offers"], [])

    async def test_install(self):
        self.light._on_ota(self.event(0x42007530))
        image = self.light._ota_image

        async def update(img, progress):
            self.assertIs(img, image)
            progress(0, 10, 50.0)
            return zigbee.Status.SUCCESS

        self.light.zdev = SimpleNamespace(update_firmware=update)
        changes = []
        self.updates.on_change = lambda: changes.append(dict(self.updates.list()))
        with (
            mock.patch.object(zigbee, "SAVE_DELAY", 0),
            mock.patch.object(zigbee, "save_snapshot") as snapshot,
        ):
            self.assertTrue(await self.updates.install("globe"))
        snapshot.assert_called_once()
        self.assertEqual(changes[0]["running"], "globe")
        self.assertEqual(changes[1]["progress"], 50)
        result = self.updates.list()
        self.assertEqual((result["offers"], result["running"]), ([], None))
        self.assertEqual(result["last"], "globe : firmware 0x42007530 installé.")

    async def test_install_refused(self):
        self.light._on_ota(self.event(0x42007530))
        update = mock.AsyncMock(return_value=zigbee.Status.FAILURE)
        self.light.zdev = SimpleNamespace(update_firmware=update)
        with mock.patch.object(zigbee, "save_snapshot") as snapshot:
            self.assertFalse(await self.updates.install("globe"))
        snapshot.assert_not_called()
        result = self.updates.list()
        self.assertEqual(len(result["offers"]), 1)  # reste proposé
        self.assertTrue(result["last"].startswith("Échec : globe : firmware refusé"))

    def test_reattach_after_reinterview(self):
        """Après une mise à jour, zigpy remplace l'objet de l'appareil : le pilote s'y rattache."""
        coordinator = zigbee.Coordinator()
        coordinator.app = SimpleNamespace()
        coordinator._devices[self.light.ieee] = self.light
        with mock.patch.object(self.light, "_attach") as attach:
            coordinator.device_reinterviewed(SimpleNamespace(ieee=self.light.ieee))
        attach.assert_called_once_with(coordinator.app)

    async def test_one_at_a_time(self):
        self.updates.running = "autre"
        with self.assertRaises(RuntimeError):
            await self.updates.install("globe")


class DeclarationTest(unittest.IsolatedAsyncioTestCase):
    def test_invalid_ieee(self):
        for ieee in ("pas-une-adresse", "a4:c1:38", ""):
            with self.subTest(ieee=ieee), self.assertRaises(ValueError):
                Plug(ieee)

    async def test_start_registers_ieee(self):
        plug = Plug(IEEE)
        plug.configure("prise")
        self.assertEqual(plug.physical(), f"Zigbee {IEEE}")
        with mock.patch.object(zigbee.coordinator, "register") as register:
            await plug.start()
        register.assert_called_once_with(plug)

    async def test_same_ieee_twice_ignored(self):
        coordinator = zigbee.Coordinator()
        first, second = Plug(IEEE), Plug(IEEE)
        first.configure("prise")
        second.configure("autre")
        with mock.patch.object(coordinator, "_launch"):
            coordinator.register(first)
            with self.assertLogs("drivers.zigbee", "ERROR") as logs:
                coordinator.register(second)
            self.assertIn("autre : IEEE", logs.output[0])
            await coordinator.unregister(second)  # ne retire pas le premier
        self.assertIs(coordinator._devices[EUI64.convert(IEEE)], first)


class UnusedTest(unittest.IsolatedAsyncioTestCase):
    """Appareils du réseau qu'aucun équipement déclaré n'utilise."""

    def setUp(self):
        self.coordinator = zigbee.Coordinator()
        plug = Plug(IEEE)
        plug.configure("prise")
        with mock.patch.object(self.coordinator, "_launch"):
            self.coordinator.register(plug)
        self.app = make_app(make_zdev(), make_zdev(OTHER, "LOM002"))

        @asynccontextmanager
        async def session():
            yield self.app

        patcher = mock.patch.object(self.coordinator, "session", session)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unused(self):
        unused = self.coordinator.unused(self.app)
        self.assertEqual([zdev.ieee for zdev in unused], [EUI64.convert(OTHER)])
        self.coordinator.paired.add(EUI64.convert(OTHER))  # appairé depuis le démarrage
        self.assertEqual(self.coordinator.unused(self.app), [])

    async def test_remove_unused(self):
        with mock.patch.object(zigbee, "save_snapshot") as snapshot:
            removed = await self.coordinator.remove_unused()
        self.app.remove.assert_awaited_once_with(EUI64.convert(OTHER))
        self.assertEqual(removed, [f"{OTHER} (Fab LOM002)"])
        snapshot.assert_called_once()

    async def test_remove_failure_skipped(self):
        self.app.remove.side_effect = OSError("clé occupée")
        with (
            mock.patch.object(zigbee, "save_snapshot") as snapshot,
            self.assertLogs("drivers.zigbee", "WARNING"),
        ):
            self.assertEqual(await self.coordinator.remove_unused(), [])
        snapshot.assert_not_called()


class SummaryTest(unittest.IsolatedAsyncioTestCase):
    """equipements.yaml : appareils du réseau non déclarés, avec leur déclaration, puis fiches."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "equipements.yaml"
        self.coordinator = zigbee.Coordinator()
        self.devices = {"prise": Plug(IEEE), "lampe": YeelightBulb(host="192.0.2.1")}
        for dev_id, device in self.devices.items():
            device.configure(dev_id)
        with mock.patch.object(self.coordinator, "_launch"):
            self.coordinator.register(self.devices["prise"])
        self.coordinator._task = mock.Mock()  # clé demandée
        unknown = make_zdev("11:22:33:44:55:66:77:88", model="inconnu")
        unknown.endpoints = {}
        self.app = make_app(make_zdev(), make_zdev(OTHER, "LOM002"), unknown)
        for target, name, value in (
            (zigbee, "coordinator", self.coordinator),
            (homekit, "PAIRING_DIR", Path(tmp.name) / "homekit"),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def lines(self):
        return self.out.read_text(encoding="utf-8").splitlines()[3:]

    DEVICES = [
        "",
        "lampe:",
        "  driver : yeelight.bulb",
        "  host   : 192.0.2.1",
        "",
        "prise:",
        "  driver : zigbee.plug",
        f"  ieee   : {IEEE}",
    ]

    async def test_written_once_key_open(self):
        with self.assertLogs("core.devices", "WARNING") as logs:
            task = asyncio.create_task(summarize(self.devices, self.out))
            await asyncio.sleep(0)
            self.assertFalse(self.out.exists())  # attend la clé
            self.coordinator.app = self.app
            self.coordinator._ready.set()
            await asyncio.wait_for(task, 1)
        self.assertEqual(
            self.lines(),
            [
                "unlinked:",
                f"  Zigbee {OTHER}:",
                "    declaration: |",
                "      from drivers.all import Plug",
                f'      <id> = Plug(ieee="{OTHER}")  # Zigbee : Fab, modèle LOM002',
                "  Zigbee 11:22:33:44:55:66:77:88:",
                "    declaration : unknown driver, to declare by hand",
            ]
            + self.DEVICES,
        )
        self.assertEqual(len(logs.output), 1)
        self.assertIn(f"2 anomalie(s) d'appairage, voir {self.out}", logs.output[0])

    def test_not_written_key_not_open(self):
        with self.assertLogs("core.devices", "INFO"):
            self.assertEqual(write_summary(self.devices, self.out), 0)
        self.assertFalse(self.out.exists())

    async def test_key_never_requested(self):
        self.coordinator._task = None
        await asyncio.wait_for(summarize(self.devices, self.out), 1)
        self.assertEqual(self.lines(), ["unlinked : none"] + self.DEVICES)


class DetailsTest(unittest.TestCase):
    """Fiche d'un appareil Zigbee : identité annoncée, firmware, état de chaque mesure."""

    def test_climate(self):
        clusters = {
            cid: SimpleNamespace(cluster_id=cid, ep_attribute=name)
            for cid, name in ((0x0001, "power"), (0x0402, "temperature"), (0x0405, "humidity"),
                              (0x040D, "carbon_dioxide_concentration"), (0xFC11, None))
        }
        ota = {"current_file_version": 0x2300}
        endpoint = SimpleNamespace(
            in_clusters=clusters, out_clusters={}, profile_id=0x0104, device_type=0x0302
        )
        green_power = SimpleNamespace(
            in_clusters={}, out_clusters={}, profile_id=0xA1E0, device_type=0x0061
        )
        sensor = Climate(IEEE, reports={"humidity": 0.2, "battery": None})
        sensor.zdev = SimpleNamespace(
            manufacturer="SONOFF", model="SNZB-02D",
            endpoints={0: SimpleNamespace(), 1: endpoint, 242: green_power},
            find_cluster=lambda _cid, _type: ota,
        )
        self.assertEqual(sensor.details(), {
            "ieee": IEEE,
            "model": "SNZB-02D [SONOFF]",
            "type": "0x0302 (HA) Temperature sensor",
            "firmware": "0x00002300 (OTA)",
            "measures": {
                "temperature": "tracked 0.1",
                "humidity": "tracked 0.2 (forced)",
                "pressure": "absent",
                "battery": "disabled (forced)",
                "carbon_dioxide_concentration": "unmanaged",
            },
            "proprietary": "0xfc11",
        })

    def test_key_closed(self):
        self.assertEqual(Plug(IEEE).details(), {"ieee": IEEE})

    def test_check_measure(self):
        sensor = Climate(IEEE, reports={"battery": None})
        self.assertEqual(sensor.check_measure("temperature"), "")
        self.assertEqual(sensor.check_measure("connected"), "")
        self.assertEqual(sensor.check_measure("pressure"), "non suivie (reports)")
        self.assertEqual(sensor.check_measure("battery"), "non suivie (reports)")
        self.assertEqual(sensor.check_measure("temperatur"), "mesure inconnue")
        humidity = SimpleNamespace(cluster_id=0x0405, ep_attribute="humidity")
        endpoint = SimpleNamespace(in_clusters={0x0405: humidity}, out_clusters={}, profile_id=0x0104)
        sensor.zdev = SimpleNamespace(endpoints={1: endpoint})  # rattaché : clusters connus
        self.assertEqual(sensor.check_measure("humidity"), "")
        self.assertEqual(sensor.check_measure("temperature"), "l'appareil ne la fournit pas")

    def test_measures(self):
        """État de surveillance : absence connue une fois rattaché (prêt)."""
        sensor = Climate(IEEE, reports={"battery": None})
        self.assertFalse(sensor.ready())
        self.assertEqual(sensor.measures(), {
            "battery": "disabled", "connected": "tracked", "humidity": "tracked",
            "pressure": "disabled", "temperature": "tracked",
        })
        humidity = SimpleNamespace(cluster_id=0x0405, ep_attribute="humidity")
        endpoint = SimpleNamespace(in_clusters={0x0405: humidity}, out_clusters={}, profile_id=0x0104)
        sensor.zdev = SimpleNamespace(endpoints={1: endpoint})
        self.assertTrue(sensor.ready())
        self.assertEqual(sensor.measures(), {
            "battery": "absent", "connected": "tracked", "humidity": "tracked",
            "pressure": "absent", "temperature": "absent",
        })


class CoordinatorSessionTest(unittest.IsolatedAsyncioTestCase):
    """L'appairage emprunte la clé : ouverte pour l'occasion, refermée ensuite."""

    async def test_opens_and_closes_key(self):
        app = make_app()
        closed = asyncio.Event()

        @asynccontextmanager
        async def session():
            try:
                yield app
            finally:
                closed.set()

        coordinator = zigbee.Coordinator()
        with mock.patch.object(zigbee, "open_session", session):
            async with coordinator.session() as borrowed:
                self.assertIs(borrowed, app)
            await asyncio.wait_for(closed.wait(), 1)
        self.assertIsNone(coordinator._task)

    async def test_key_unavailable(self):
        @asynccontextmanager
        async def session():
            raise OSError("port absent")
            yield

        coordinator = zigbee.Coordinator()
        with (
            mock.patch.object(zigbee, "open_session", session),
            mock.patch.object(zigbee, "READY_WAIT", 0.05),
            self.assertLogs("drivers.zigbee", "WARNING"),
        ):
            with self.assertRaises(zigbee.ERRORS):
                async with coordinator.session():
                    pass
        self.assertIsNone(coordinator._task)


class FakeListener:
    def __init__(self):
        self.closed = False

    async def shutdown(self):
        self.closed = True


class FakeApp:
    """Coordinateur dont l'arrêt bloque à la déconnexion de la clé (clé retirée)."""

    def __init__(self):
        self._dblistener = FakeListener()
        self.listener = self._dblistener
        self.stuck = asyncio.Event()

    def _remove_db_listeners(self):
        self.listener = None

    async def shutdown(self, *, db=True):
        self.stuck.set()
        await asyncio.Event().wait()


class CloseSessionTest(unittest.IsolatedAsyncioTestCase):
    """Arrêt du coordinateur bloqué : la base est refermée quand même."""

    async def test_stuck_shutdown_gives_up(self):
        app = FakeApp()
        with (
            mock.patch.object(zigbee, "CLOSE_WAIT", 0.05),
            self.assertLogs("drivers.zigbee", "WARNING") as logs,
        ):
            await zigbee.close_session(app)
        self.assertTrue(app._dblistener.closed)
        self.assertIsNone(app.listener)
        self.assertIn("FakeApp.shutdown", logs.output[0])

    async def test_normal_shutdown(self):
        app = FakeApp()
        app.shutdown = mock.AsyncMock()
        with self.assertNoLogs("drivers.zigbee", "WARNING"):
            await zigbee.close_session(app)
        app.shutdown.assert_awaited_once_with(db=False)
        self.assertTrue(app._dblistener.closed)

    async def test_cancelled_while_stuck(self):
        app = FakeApp()
        task = asyncio.create_task(zigbee.close_session(app))
        await app.stuck.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(app._dblistener.closed)


class PairProtocolsTest(unittest.TestCase):
    def test_protocols_found(self):
        self.assertEqual(set(core_pair.protocols()), {"homekit", "zigbee"})

    def test_id_from_name(self):
        self.assertEqual(id_from_name("Prise salon"), "prise_salon")
        self.assertEqual(id_from_name("Lampe d'entrée"), "lampe_d_entree")


class PairRunTest(unittest.IsolatedAsyncioTestCase):
    """Déroulé de l'appairage Zigbee, clé simulée : l'appareil s'est appairé."""

    def setUp(self):
        self.declared = {}  # équipements déclarés dans devices/
        patcher = mock.patch.object(core_pair, "load_devices", lambda: self.declared)
        patcher.start()
        self.addCleanup(patcher.stop)
        paired = mock.patch.object(zigbee.coordinator, "paired", set())
        self.paired = paired.start()
        self.addCleanup(paired.stop)

    async def pair(self, *answers, model="TS011F"):
        device = make_zdev(model=model)

        @asynccontextmanager
        async def session():
            yield SimpleNamespace(devices={device.ieee: device})

        with (
            mock.patch.object(zigbee.coordinator, "session", session),
            mock.patch.object(zigbee_pair, "wait_device", mock.AsyncMock(return_value=device)),
            mock.patch.object(zigbee_pair, "SETTLE", 0),
            mock.patch.object(zigbee_pair, "save_snapshot") as snapshot,
            mock.patch.object(core_pair, "add_device") as add,
        ):
            ok = await zigbee_pair.Pairing(ScriptedDialog(*answers)).run(1)
        snapshot.assert_called_once()
        self.assertEqual(self.paired, {EUI64.convert(IEEE)})
        return ok, add

    async def test_known_device_keeps_id(self):
        self.declared["prise"] = Plug(IEEE)
        ok, add = await self.pair()  # aucune question
        self.assertTrue(ok)
        add.assert_not_called()

    async def test_new_device_declared_with_ieee(self):
        ok, add = await self.pair("", "Prise bureau")  # identifiant vide : redemandé
        self.assertTrue(ok)
        self.assertEqual(add.call_args.args, ("prise_bureau", "zigbee.plug"))
        self.assertEqual(add.call_args.kwargs["args"], (f'ieee="{IEEE}"',))

    async def test_existing_id_refused(self):
        self.declared["prise"] = Plug(OTHER)  # autre appareil
        ok, add = await self.pair("Prise", "Prise 2")
        self.assertTrue(ok)
        self.assertEqual(add.call_args.args[0], "prise_2")

    async def test_driver_from_device_type(self):
        ok, add = await self.pair("Ventilateur", model="LOM002")  # modèle absent de la table
        self.assertTrue(ok)
        self.assertEqual(add.call_args.args, ("ventilateur", "zigbee.plug"))


if __name__ == "__main__":
    unittest.main()
