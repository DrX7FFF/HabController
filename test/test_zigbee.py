"""Tests Zigbee sans clé : instantané, correspondance et synchro, état de la prise, pair."""

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
from core import pair as pair_cli  # noqa: E402
import drivers.zigbee.pair as zigbee_pair  # noqa: E402
import drivers.zigbee.plug as zigbee_plug  # noqa: E402
import drivers.homekit as homekit  # noqa: E402
from core.devices import id_from_name, write_summary  # noqa: E402
from drivers.yeelight.bulb import YeelightBulb  # noqa: E402
from core import base  # noqa: E402
from drivers.zigbee.light import ZigbeeLight, rgb_to_xy, xy_to_rgb  # noqa: E402
from drivers.zigbee.plug import ELECTRICAL, METERING, ON_OFF, Plug  # noqa: E402
from drivers.zigbee.button import Button  # noqa: E402
from drivers.zigbee.sensor import Sensor  # noqa: E402
from zigpy.types import EUI64  # noqa: E402

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


class PlugStateTest(unittest.TestCase):
    def make(self):
        plug = Plug(name="Prise")
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
        plug = Plug(name="Prise")
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
        sensor = Sensor(name="Météo")
        sensor.zdev = SimpleNamespace(skip_configuration=True)
        self.assertEqual(sensor.state(), {"connected": False})
        self.report(sensor, 0x0402, "measured_value", 2150)
        self.report(sensor, 0x0001, "battery_percentage_remaining", 170)
        self.report(sensor, 0x0400, "measured_value", 10001)
        self.report(sensor, 0x0006, "on_off", 1)
        expected = {"temperature": 21.5, "battery": 85, "illuminance": 10, "open": True}
        self.assertEqual(sensor.state(), {"connected": True, **expected})

    async def test_button_relays_quirk_events(self):
        button = Button(name="Bouton")
        button.zdev = SimpleNamespace(skip_configuration=True)
        got = []
        button.subscribe_events(lambda name, value: got.append((name, value)))
        button.zha_send_event("double", [])
        button.zha_send_event("rotate_right", {"relative_degrees": 90})
        self.assertEqual(got, [("double", None), ("rotate_right", {"relative_degrees": 90})])
        self.assertTrue(button.connected)

    async def test_no_poll_on_attach(self):
        sensor = Sensor(name="Météo")
        sensor.ieee = EUI64.convert(IEEE)
        endpoint = SimpleNamespace(in_clusters={0x0402: mock.Mock()}, out_clusters={})
        zdev = SimpleNamespace(endpoints={0: None, 1: endpoint})
        with mock.patch.object(zigbee, "ZigpyDevice", SimpleNamespace):
            sensor._attach(SimpleNamespace(devices={sensor.ieee: zdev}))
        self.assertFalse(sensor._tasks)


class LightTest(unittest.IsolatedAsyncioTestCase):
    def test_state_from_zcl(self):
        light = ZigbeeLight(name="Globe")
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
        light = ZigbeeLight(name="Plafonnier")
        light.zdev = SimpleNamespace()
        with mock.patch("drivers.zigbee.light.find_cluster", return_value=None):
            with self.assertRaises(ValueError):
                await light.set("rgb", "#ff0000")

    def white_light(self, caps):
        """Ampoule à clusters marche/arrêt, intensité, couleur ; color_capabilities = caps."""
        light = ZigbeeLight(name="Chambre")
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


class MappingTestCase(unittest.IsolatedAsyncioTestCase):
    """pairing/zigbee.toml redirigé dans un dossier temporaire, équipements déclarés simulés."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mapping = Path(tmp.name) / "pairing" / "zigbee.toml"
        self.declared = {}  # équipements déclarés dans devices/
        for target, name, value in (
            (zigbee, "MAPPING", self.mapping),
            (pair_cli, "load_devices", lambda: self.declared),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def link(self, **links):
        """Écrit la correspondance {id: ieee}."""
        mapping = {EUI64.convert(ieee): dev_id for dev_id, ieee in links.items()}
        zigbee.save_mapping(mapping, {})


class MappingTest(MappingTestCase):
    def test_save_then_load(self):
        device = make_zdev()
        zigbee.save_mapping({device.ieee: "prise"}, {device.ieee: device})
        text = self.mapping.read_text(encoding="utf-8")
        self.assertIn(f'"{IEEE}" = "prise"  # Fab TS011F', text)
        self.assertEqual(zigbee.load_mapping(), {EUI64.convert(IEEE): "prise"})
        self.assertEqual(zigbee.linked_ieee("prise"), EUI64.convert(IEEE))
        with self.assertRaises(zigbee.PairingError):
            zigbee.linked_ieee("autre")

    def test_missing_or_invalid(self):
        with self.assertRaises(zigbee.PairingError):
            zigbee.load_mapping()
        self.assertEqual(zigbee.load_mapping(missing_ok=True), {})
        self.mapping.parent.mkdir(parents=True)
        for text in ('"pas-une-adresse" = "prise"\n', f'"{IEEE}" = 3\n', "casse ="):
            self.mapping.write_text(text, encoding="utf-8")
            with self.assertRaises(zigbee.PairingError):
                zigbee.load_mapping(missing_ok=True)

    async def test_start_without_pairing_stays_unreachable(self):
        plug = Plug(name="Prise")
        plug.configure("prise")
        with mock.patch.object(zigbee.coordinator, "register") as register:
            await plug.start()
        register.assert_not_called()
        self.assertEqual(plug.state(), {"connected": False})

    async def test_start_reads_ieee_from_mapping(self):
        self.link(prise=IEEE)
        plug = Plug(name="Prise")
        plug.configure("prise")
        with mock.patch.object(zigbee.coordinator, "register") as register:
            await plug.start()
        register.assert_called_once_with(plug, EUI64.convert(IEEE))


class SynchronizeTest(MappingTestCase):
    """Base alignée sur pairing/zigbee.toml à l'ouverture de la clé."""


    def app(self, *devices):
        coordinator = SimpleNamespace(ieee=EUI64.convert("00:00:00:00:00:00:00:01"))
        return SimpleNamespace(
            devices={device.ieee: device for device in (coordinator, *devices)},
            state=SimpleNamespace(node_info=SimpleNamespace(ieee=coordinator.ieee)),
            remove=mock.AsyncMock(),
        )

    async def test_missing_mapping_removes_nothing(self):
        app = self.app(make_zdev())
        with self.assertLogs("drivers.zigbee", "WARNING"):
            await zigbee.synchronize(app)
        app.remove.assert_not_called()

    async def test_unlinked_device_removed(self):
        self.link(prise=IEEE)
        self.declared["prise"] = Plug(name="Prise")
        app = self.app(make_zdev(), make_zdev(OTHER, "LOM002"))
        with self.assertLogs("drivers.zigbee", "WARNING") as logs:
            await zigbee.synchronize(app)
        app.remove.assert_awaited_once_with(EUI64.convert(OTHER))
        self.assertEqual(len(logs.output), 1)

    async def test_alerts(self):
        self.link(prise=IEEE, ventilateur=OTHER)  # ventilateur absent de la base
        app = self.app(make_zdev())  # prise non déclarée
        with self.assertLogs("drivers.zigbee", "WARNING") as logs:
            await zigbee.synchronize(app)
        app.remove.assert_not_called()
        text = "\n".join(logs.output)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ventilateur : déclaré avec l'IEEE", text)


class SummaryTest(MappingTestCase):
    def test_logical_with_physical_then_orphans(self):
        self.link(prise=IEEE, ventilateur=OTHER)
        devices = {"prise": Plug(name="Prise"), "lampe": YeelightBulb(host="192.0.2.1")}
        for dev_id, device in devices.items():
            device.configure(dev_id)
        devices["seule"] = Plug()  # déclarée, jamais appairée
        devices["seule"].configure("seule")
        out = self.mapping.parent / "equipements.txt"
        models = {"ventilateur": (["LOM002", "0x010a"], "Zigbee : Philips, modèle LOM002")}
        with (
            mock.patch.object(homekit, "PAIRING_DIR", self.mapping.parent / "homekit"),
            mock.patch.object(zigbee, "link_models", lambda: models),
            self.assertLogs("core.devices", "WARNING") as logs,
        ):
            write_summary(devices, out)
        self.assertEqual(len(logs.output), 1)
        self.assertIn(f"2 anomalie(s) d'appairage, voir {out}", logs.output[0])
        lines = out.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines[2:],
            [
                "id     nom    pilote         physique",
                "lampe  lampe  yeelight.bulb  Yeelight 192.0.2.1",
                "prise  Prise  zigbee.plug    Zigbee a4:c1:38:d6:d7:c6:db:f6",
                "seule  seule  zigbee.plug    -> non appairé : ./habctrl pair zigbee",
                "",
                "# Appareils physiques non liés (équipement absent de devices/)",
                "",
                f"-> ventilateur  Zigbee {OTHER}  à déclarer dans un fichier de devices/ ainsi :",
                "from drivers.zigbee.plug import Plug",
                'ventilateur = Plug(name="Ventilateur")  # Zigbee : Philips, modèle LOM002',
            ],
        )


class PairCommandTest(unittest.TestCase):
    def test_protocols_found(self):
        self.assertEqual(set(pair_cli.protocols()), {"homekit", "zigbee"})

    def test_id_from_name(self):
        self.assertEqual(id_from_name("Prise salon"), "prise_salon")
        self.assertEqual(id_from_name("Lampe d'entrée"), "lampe_d_entree")


class PairRunTest(MappingTestCase):
    """Déroulé de ./habctrl pair zigbee, clé simulée : l'appareil s'est appairé."""

    async def pair(self, *answers, model="TS011F"):
        device = make_zdev(model=model)

        @asynccontextmanager
        async def coordinator():
            yield SimpleNamespace(devices={device.ieee: device})

        with (
            mock.patch.object(zigbee, "open_session", coordinator),
            mock.patch.object(zigbee_pair, "joined_devices", return_value=[]),
            mock.patch.object(zigbee_pair, "wait_device", mock.AsyncMock(return_value=device)),
            mock.patch.object(zigbee_pair, "SETTLE", 0),
            mock.patch.object(zigbee_pair, "save_snapshot") as snapshot,
            mock.patch.object(pair_cli, "add_device") as add,
            mock.patch("builtins.input", side_effect=answers),
            mock.patch("builtins.print"),
        ):
            code = await zigbee_pair.Pairing().run(1)
        snapshot.assert_called_once()
        return code, add

    async def test_known_device_keeps_name(self):
        self.link(prise=IEEE)
        self.declared["prise"] = Plug(name="Prise")
        code, add = await self.pair("")  # Entrée : garder le nom
        self.assertEqual(code, 0)
        add.assert_not_called()
        self.assertEqual(zigbee.load_mapping(), {EUI64.convert(IEEE): "prise"})

    async def test_known_device_renamed(self):
        self.link(prise=IEEE)
        code, add = await self.pair("n", "", "Prise bureau")  # nom vide : redemandé
        self.assertEqual(code, 0)
        self.assertEqual(zigbee.load_mapping(), {EUI64.convert(IEEE): "prise_bureau"})
        self.assertEqual(add.call_args.args[:2], ("prise_bureau", "zigbee.plug"))
        self.assertEqual(add.call_args.kwargs["name"], "Prise bureau")

    async def test_replaces_dead_device(self):
        self.link(prise="00:11:22:33:44:55:66:77")  # ancien appareil, HS
        self.declared["prise"] = Plug(name="Prise")
        code, add = await self.pair("Prise", "n", "Prise", "o")  # refus, puis remplacement
        self.assertEqual(code, 0)
        add.assert_not_called()  # déclaration gardée
        self.assertEqual(zigbee.load_mapping(), {EUI64.convert(IEEE): "prise"})

    async def test_driver_from_device_type(self):
        code, add = await self.pair("Ventilateur", model="LOM002")  # modèle absent de la table
        self.assertEqual(code, 0)
        self.assertEqual(add.call_args.args[:2], ("ventilateur", "zigbee.plug"))
        self.assertIn("# Fab LOM002", self.mapping.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
