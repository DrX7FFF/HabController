import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.widgets import load_blocks  # noqa: E402


class LoadBlocksTest(unittest.TestCase):
    def test_unknown_device_or_widget_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "screens.toml"
            path.write_text(
                "[[blocks]]\ndevice = \"salon\"\nwidget = \"onoff\"\n"
                "[[blocks]]\ndevice = \"fantome\"\nwidget = \"onoff\"\n"
                "[[blocks]]\ndevice = \"salon\"\nwidget = \"inconnu\"\n",
                encoding="utf-8",
            )
            with self.assertLogs("core.widgets", "ERROR") as logs:
                blocks = load_blocks({"salon": SimpleNamespace(id="salon")}, path)
        self.assertEqual(blocks, [{"title": "salon", "widget": "onoff", "device": "salon"}])
        self.assertEqual(len(logs.output), 2)


if __name__ == "__main__":
    unittest.main()
