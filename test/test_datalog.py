import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import base, datalog  # noqa: E402
from core.base import Device  # noqa: E402


class FakeDevice(Device):
    def __init__(self, dev_id, state=None):
        super().__init__(dev_id)
        self.configure(dev_id)
        self._state = dict(state or {})

    def state(self):
        return dict(self._state)

    def update(self, state):
        self._state = state
        self._notify()


CONF = {
    "url": "https://base.example/",
    "credentials": "absent.json",
    "retention": "30d",
    "devices": {
        "prise": {"on": {}, "active_power": {"hysteresis": 5, "retention": "1y"}},
        "fp2": {"zone.Global": {}},
    },
    "purge": {"deshum/dewPoint": "48h"},
}


class DatalogTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.prise = FakeDevice("prise")
        self.fp2 = FakeDevice("fp2")
        self.log = datalog.Datalog(CONF, {"prise": self.prise, "fp2": self.fp2})
        self.requests: list[tuple] = []
        self.answers: list = []  # réponses successives du faux Firebase (exception = échec)
        patcher = mock.patch.object(self.log, "_request", self.fake_request)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def fake_request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def idle(self):
        await asyncio.gather(*list(base.tasks))

    def written(self) -> list[dict]:
        return [kw["json"] for method, path, kw in self.requests if method == "PATCH"]


class RecordDevicesTest(DatalogTestCase):
    async def test_changes_recorded_with_device_timestamp(self):
        self.prise.update({"connected": True, "on": True, "active_power": 10.0, "voltage": 230})
        await self.idle()
        at = self.prise.changed_at
        # un seul envoi pour l'événement ; voltage n'est pas dans la table
        self.assertEqual(
            self.written(),
            [{f"devices/prise/on/{at}": True, f"devices/prise/active_power/{at}": 10.0}],
        )

    async def test_unchanged_and_hysteresis(self):
        self.prise.update({"connected": True, "on": True, "active_power": 10.0})
        self.prise.update({"connected": True, "on": True, "active_power": 14.0})  # écart < 5
        self.prise.update({"connected": True, "on": False, "active_power": 15.0})  # écart = 5
        await self.idle()
        at = self.prise.changed_at
        self.assertEqual(
            self.written()[1],
            {f"devices/prise/on/{at}": False, f"devices/prise/active_power/{at}": 15.0},
        )
        self.assertEqual(len(self.written()), 2)

    async def test_unreachable_records_nothing(self):
        self.prise.update({"connected": True, "on": True})
        self.prise.update({"connected": False})
        self.prise.update({"connected": True, "on": True})  # même valeur au retour : rien
        await self.idle()
        self.assertEqual(len(self.written()), 1)

    async def test_dotted_attribute_becomes_path(self):
        self.fp2.update({"connected": True, "zone.Global": True})
        await self.idle()
        self.assertEqual(self.written(), [{f"devices/fp2/zone/Global/{self.fp2.changed_at}": True}])


class RecordFunctionTest(DatalogTestCase):
    async def test_record_with_and_without_timestamp(self):
        with mock.patch.object(datalog, "_datalog", self.log):
            datalog.record("/deshum/dewPoint/", 12.4, at=1000)
            datalog.record("system/load", {"avg1": 0.4, "avg5": 0.3})
            await self.idle()
        first, second = self.written()
        self.assertEqual(first, {"deshum/dewPoint/1000": 12.4})
        ((key, value),) = second.items()
        self.assertRegex(key, r"^system/load/\d{13}$")
        self.assertEqual(value, {"avg1": 0.4, "avg5": 0.3})
        self.assertEqual(self.log.retention["system/load"], datalog.duration_ms("30d"))

    def test_record_without_configuration_is_ignored(self):
        with mock.patch.object(datalog, "_datalog", None):
            datalog.record("x", 1)  # ne lève rien

    async def test_failure_logged_once_then_recovery(self):
        self.answers = [OSError("réseau"), OSError("réseau"), None]
        with self.assertLogs("core.datalog", "INFO") as logs:
            for value in (1, 2, 3):
                self.log.record("x", value, None)
                await self.idle()
        self.assertEqual(len(logs.output), 2)
        self.assertIn("envoi impossible", logs.output[0])
        self.assertIn("envoi rétabli", logs.output[1])


class PurgeTest(DatalogTestCase):
    async def test_old_values_deleted_per_path(self):
        self.answers = [{"1": 5.0, "2": 6.0}]  # premier chemin : deux valeurs trop vieilles
        with mock.patch.object(datalog, "now_ms", return_value=10**12):
            await self.log.purge()
        method, path, kwargs = self.requests[0]
        self.assertEqual((method, path), ("GET", "deshum/dewPoint"))
        limit = 10**12 - 48 * 3600 * 1000
        self.assertEqual(kwargs["params"], {"orderBy": '"$key"', "endAt": f'"{limit}"'})
        self.assertEqual(
            self.requests[1], ("PATCH", "deshum/dewPoint", {"json": {"1": None, "2": None}})
        )
        purged = [path for method, path, kw in self.requests if method == "GET"]
        self.assertEqual(
            purged,
            [
                "deshum/dewPoint",
                "devices/prise/on",
                "devices/prise/active_power",
                "devices/fp2/zone/Global",
            ],
        )


class ReadTest(DatalogTestCase):
    async def test_points_sorted_with_state_at_start(self):
        self.answers = [{"300": 2.0, "200": 1.0}, {"50": 9.0}]
        points = await self.log.read("/devices/prise/active_power/", 100, 400)
        self.assertEqual(points, [[100, 9.0], [200, 1.0], [300, 2.0]])
        (_, path, inside), (_, _, before) = self.requests
        self.assertEqual(path, "devices/prise/active_power")
        self.assertEqual(inside["params"], {"orderBy": '"$key"', "startAt": '"100"', "endAt": '"400"'})
        self.assertEqual(
            before["params"], {"orderBy": '"$key"', "endAt": '"99"', "limitToLast": "1"}
        )

    async def test_empty_or_point_at_start(self):
        self.answers = [None, None]
        self.assertEqual(await self.log.read("devices/prise/on", 100, 400), [])
        self.answers = [{"100": True}, {"50": False}]
        self.assertEqual(await self.log.read("devices/prise/on", 100, 400), [[100, True]])


class ConfigTest(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(datalog.duration_ms("48h"), 48 * 3600 * 1000)
        self.assertEqual(datalog.duration_ms("1y"), 365 * 86400 * 1000)
        with self.assertRaises(ValueError):
            datalog.duration_ms("3 semaines")

    def test_unknown_device_is_an_error(self):
        with self.assertRaises(ValueError):
            datalog.Datalog({**CONF, "devices": {"fantome": {"on": {}}}}, {})

    def test_retention_per_attribute(self):
        log = datalog.Datalog(CONF, {"prise": FakeDevice("prise"), "fp2": FakeDevice("fp2")})
        self.assertEqual(log.retention["devices/prise/active_power"], datalog.duration_ms("1y"))
        self.assertEqual(log.retention["devices/prise/on"], datalog.duration_ms("30d"))


if __name__ == "__main__":
    unittest.main()
