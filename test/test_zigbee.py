"""Tests Zigbee sans clé : instantané de la base, état de la prise, commande d'appairage."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import drivers.zigbee as zigbee  # noqa: E402
from core import pair as pair_cli  # noqa: E402
from drivers.zigbee.plug import ELECTRICAL, METERING, ON_OFF, Plug  # noqa: E402

IEEE = "a4:c1:38:d6:d7:c6:db:f6"


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
        plug = Plug(ieee=IEEE, name="Prise")
        plug._scales = {"active_power": 1, "voltage": 1, "current": 1 / 1000, "energy": 1 / 100}
        return plug

    def test_values_scaled_and_hidden_while_unreachable(self):
        plug = self.make()
        plug._on_attribute(ON_OFF, "on_off", True)
        plug._on_attribute(ELECTRICAL, "rms_current", 45)
        plug._on_attribute(METERING, "current_summ_delivered", 10702)
        self.assertEqual(plug.state(), {"connected": False, "power": "unreachable"})
        plug._connected = plug._synced = True
        self.assertEqual(
            plug.state(),
            {"connected": True, "power": "on", "current": 0.045, "energy": 107.02},
        )

    def test_measure_without_cluster_is_ignored(self):
        plug = self.make()
        plug._scales = {}
        plug._on_attribute(ELECTRICAL, "active_power", 12)
        plug._connected = plug._synced = True
        self.assertNotIn("active_power", plug.state())

    def test_invalid_ieee(self):
        with self.assertRaises(ValueError):
            Plug(ieee="pas-une-adresse")


class PairCommandTest(unittest.TestCase):
    def test_protocols_found(self):
        self.assertEqual(set(pair_cli.protocols()), {"homekit", "zigbee"})


if __name__ == "__main__":
    unittest.main()
