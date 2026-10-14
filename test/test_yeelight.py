import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import base  # noqa: E402
from drivers.yeelight.bulb import YeelightBulb  # noqa: E402


class FakeBulb:
    def __init__(self):
        self.calls = []

    async def async_turn_on(self, **fx):
        self.calls.append("on")

    async def async_turn_off(self, **fx):
        self.calls.append("off")

    async def async_set_scene(self, scene_class, flow):
        self.calls.append((scene_class.name, *flow.as_start_flow_params))


class YeelightOnTest(unittest.IsolatedAsyncioTestCase):
    def make(self):
        bulb = YeelightBulb(host="192.0.2.1", name="Salon")
        bulb._connected = bulb._synced = True
        bulb._bulb = FakeBulb()  # type: ignore[assignment]
        return bulb

    def test_protocol_power_becomes_boolean(self):
        bulb = self.make()
        self.assertEqual(bulb.state(), {"connected": True})  # pas encore lu : pas d'attribut on
        bulb._merge({"power": "on", "bright": "40"})
        self.assertEqual(bulb.state(), {"connected": True, "on": True, "brightness": 40})
        bulb._merge({"power": "off"})
        self.assertIs(bulb.on, False)
        bulb._connected = False
        self.assertEqual(bulb.state(), {"connected": False})

    async def test_set_on_and_toggle(self):
        bulb = self.make()
        bulb._merge({"power": "off"})
        await bulb.set("on", "toggle", 0)
        self.assertIs(bulb.on, True)
        await bulb.set("on", False, 0)
        self.assertIs(bulb.on, False)
        self.assertEqual(bulb._bulb.calls, ["on", "off"])

    async def idle(self):
        await asyncio.gather(*list(base.tasks))

    async def test_set_rgb_and_set_temp_send_one_message(self):
        bulb = self.make()
        bulb._merge({"power": "off"})
        bulb.set_rgb(80, "#ff0000", transition=0)
        bulb.set_temp(60, 2700, transition=2000)
        await self.idle()
        # une scène « color flow » d'une étape (1 fois, rester) : durée, mode, valeur, intensité
        self.assertEqual(
            bulb._bulb.calls,
            [("CF", 1, 1, "50, 1, 16711680, 80"), ("CF", 1, 1, "2000, 2, 2700, 60")],
        )
        self.assertEqual(
            bulb.state(),
            {"connected": True, "on": True, "brightness": 60, "color_temp": 2700, "rgb": "#ff0000"},
        )

    async def test_default_transition_and_toggle(self):
        bulb = self.make()
        bulb._merge({"power": "on"})
        bulb.set_temp(50, 4000)
        bulb.toggle()
        await self.idle()
        self.assertEqual(bulb._bulb.calls, [("CF", 1, 1, "300, 2, 4000, 50"), "off"])
        self.assertIs(bulb.on, False)

    async def test_unreachable_is_logged(self):
        bulb = self.make()
        bulb._connected = False
        with self.assertLogs("core.base", "WARNING") as logs:
            bulb.set_rgb(80, "#ff0000")
            await self.idle()
        self.assertIn("injoignable", logs.output[0])
        self.assertEqual(bulb._bulb.calls, [])


if __name__ == "__main__":
    unittest.main()
