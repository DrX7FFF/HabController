import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.charts import load_charts  # noqa: E402
from core.history_mock import mock_history  # noqa: E402

CONFIG = Path(__file__).resolve().parent / "config" / "dashboard" / "charts.toml"
HOUR = 3_600_000


def charts_from(text: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "charts.toml"
        path.write_text(text, encoding="utf-8")
        return load_charts(path)


class LoadChartsTest(unittest.TestCase):
    def test_test_config(self):
        charts = load_charts(CONFIG)
        self.assertEqual(charts[0]["title"], "Prise salon")
        power, on = charts[0]["series"]
        self.assertEqual((power["unit"], power["type"]), ("W", "number"))
        self.assertEqual(on["type"], "bool")

    def test_missing_file(self):
        self.assertEqual(load_charts(Path("/nulle/part/charts.toml")), [])

    def test_defaults(self):
        charts = charts_from('[[charts]]\nseries = [{ path = "/devices/fp2/illuminance/" }]')
        self.assertEqual(
            charts[0]["series"][0],
            {"path": "devices/fp2/illuminance", "label": "illuminance", "unit": "", "type": "number"},
        )

    def test_errors(self):
        for text in (
            '[[charts]]\ntitle = "vide"',
            '[[charts]]\nseries = [{ label = "sans chemin" }]',
            '[[charts]]\nseries = [{ path = "a", type = "texte" }]',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                charts_from(text)


class MockHistoryTest(unittest.TestCase):
    def test_numbers_in_period(self):
        points = mock_history("devices/prise_salon/active_power", "number", 10 * HOUR, 14 * HOUR)
        self.assertTrue(all(10 * HOUR - 300_000 < t <= 14 * HOUR for t, _ in points))
        self.assertTrue(all(isinstance(v, float) for _, v in points))

    def test_bools_are_changes(self):
        points = mock_history("devices/salon/on", "bool", 0, 48 * HOUR)
        self.assertEqual(points[0][0], 0)  # état au début de la période
        values = [v for _, v in points]
        self.assertTrue(all(a != b for a, b in zip(values, values[1:])))

    def test_stable_across_requests(self):
        a = mock_history("devices/salon/on", "bool", 0, 48 * HOUR)
        b = mock_history("devices/salon/on", "bool", 24 * HOUR, 48 * HOUR)
        state = [v for t, v in a if t <= 24 * HOUR][-1]
        self.assertEqual(b[0], [24 * HOUR, state])
        self.assertEqual([p for p in a if p[0] > 24 * HOUR], b[1:])


if __name__ == "__main__":
    unittest.main()
