import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.charts import downsample, load_charts  # noqa: E402

CONFIG = Path(__file__).resolve().parent / "config" / "dashboard" / "charts.toml"


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
        """Graphique incorrect : ignoré, erreur au journal ; les autres sont gardés."""
        valid = '[[charts]]\ntitle = "ok"\nseries = [{ path = "b" }]\n'
        for text in (
            '[[charts]]\ntitle = "vide"\n',
            '[[charts]]\nseries = [{ label = "sans chemin" }]\n',
            '[[charts]]\nseries = [{ path = "a", type = "texte" }]\n',
        ):
            with self.subTest(text=text), self.assertLogs("core.charts", "ERROR"):
                charts = charts_from(text + valid)
                self.assertEqual([c["title"] for c in charts], ["ok"])

    def test_unreadable_file(self):
        with self.assertLogs("core.charts", "ERROR"):
            self.assertEqual(charts_from("[[charts]\n"), [])


class DownsampleTest(unittest.TestCase):
    def test_short_series_unchanged(self):
        points = [[t, t % 7] for t in range(10)]
        self.assertIs(downsample(points, 10), points)

    def test_keeps_peaks_in_order(self):
        points = [[t, 1.0] for t in range(10_000)]
        points[5_000][1] = 99.0
        reduced = downsample(points, 100)
        self.assertLessEqual(len(reduced), 100)
        self.assertIn([5_000, 99.0], reduced)
        self.assertEqual(reduced, sorted(reduced))
        self.assertEqual((reduced[0][0], reduced[-1][0]), (0, 9_999))


if __name__ == "__main__":
    unittest.main()
