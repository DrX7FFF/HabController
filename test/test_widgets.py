import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.widgets import load_blocks  # noqa: E402


class LoadBlocksTest(unittest.TestCase):
    def test_widget_of_device(self):
        """Le widget est celui de l'équipement ; inconnu ou sans widget : bloc ignoré."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "screens.toml"
            path.write_text(
                "[[blocks]]\ndevice = \"salon\"\n"
                "[[blocks]]\ndevice = \"fantome\"\n"
                "[[blocks]]\ndevice = \"bouton\"\n",
                encoding="utf-8",
            )
            devices = {
                "salon": SimpleNamespace(id="salon", WIDGET="light"),
                "bouton": SimpleNamespace(id="bouton", WIDGET=""),
            }
            with self.assertLogs("core.widgets", "ERROR") as logs:
                blocks = load_blocks(devices, path)
        self.assertEqual(blocks, [{"title": "salon", "widget": "light", "device": "salon"}])
        self.assertEqual(len(logs.output), 2)


    def test_unreadable_file_or_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "screens.toml"
            path.write_text("[[blocks]\n", encoding="utf-8")
            with self.assertLogs("core.widgets", "ERROR"):
                self.assertEqual(load_blocks({}, path), [])
            path.write_text("[[blocks]]\ntitle = \"Salon\"\n", encoding="utf-8")
            with self.assertLogs("core.widgets", "ERROR"):
                self.assertEqual(load_blocks({}, path), [])
            self.assertEqual(load_blocks({}, Path(tmp) / "absent.toml"), [])


if __name__ == "__main__":
    unittest.main()
