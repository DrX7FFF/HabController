"""Tests hors ligne : python -m unittest discover -s test -p "test_automation.py" -v"""

import asyncio
import importlib
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

# configuration de test (test/config/), jamais celle de l'installation
os.environ["HABCTRL_CONFIG"] = str(Path(__file__).resolve().parent / "config")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_fp2 import FakePairing, make_fp2, presence_key, until  # noqa: E402

from core import automation  # noqa: E402
from core.base import Attribute, Device, DeviceError  # noqa: E402


class FakeDevice(Device):
    def __init__(self, id, state=None):
        super().__init__(id)
        self.configure(id)
        self._state = dict(state or {})
        self.commands: list[tuple] = []
        self.fail: Exception | None = None
        self.delays: list[float] = []  # attente simulée, consommée une commande à la fois

    def state(self):
        return dict(self._state)

    def update(self, **values):
        self._state.update(values)
        self._notify()

    def drop(self, *names):
        for name in names:
            self._state.pop(name, None)
        self._notify()

    async def set(self, name, value, transition=None):
        if self.delays:
            await asyncio.sleep(self.delays.pop(0))
        if self.fail:
            raise self.fail
        self.commands.append((name, value))
        self._state[name] = value
        self._notify()


class FakeBulb(FakeDevice):
    on = Attribute[bool]()
    bright = Attribute[int]()


class AutomationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        automation._handlers.clear()

    async def asyncTearDown(self):
        await automation.stop_automations()
        automation._handlers.clear()

    async def idle(self):
        while automation._tasks:  # les tâches peuvent en lancer d'autres (commandes)
            await asyncio.gather(*list(automation._tasks), return_exceptions=True)


class OnEventTest(AutomationTestCase):
    async def test_each_event_triggers(self):
        bouton = FakeDevice("bouton", {"connected": True})
        got = []

        @automation.on_event("bouton.double")
        async def double(event):
            got.append(("double", event.new))

        @automation.on_event("bouton")
        async def tous(event):
            got.append(("tous", event.attr))

        automation.bind({"bouton": bouton})
        bouton._emit("double")
        bouton._emit("double")  # même événement : déclenche encore
        bouton._emit("rotate_right", {"relative_degrees": 90})
        await self.idle()
        self.assertEqual(
            sorted(got),
            sorted([("double", None)] * 2 + [("tous", "double")] * 2 + [("tous", "rotate_right")]),
        )


class OnChangeTest(AutomationTestCase):
    async def test_attribute_event(self):
        fp = FakeDevice("fp2", {"connected": False})
        events = []

        @automation.on_change("fp2.presence_0")
        async def handler(event):
            events.append(event)

        automation.bind({"fp2": fp})
        fp.update(connected=True, presence_0=False)  # premier état connu
        await self.idle()
        fp.update(presence_0=True)
        await self.idle()
        fp.update(presence_0=True)  # inchangé : rien
        fp.update(connected=False)  # autre attribut : rien pour ce handler
        await self.idle()
        self.assertEqual(
            [(e.old, e.new) for e in events],
            [(None, False), (False, True)],
        )
        self.assertEqual(events[1].changed, {"presence_0"})
        self.assertEqual((events[1].device, events[1].attr), ("fp2", "presence_0"))

    async def test_event_carries_device_timestamp(self):
        fp = FakeDevice("fp2", {"n": 0})
        events = []

        @automation.on_change("fp2.n")
        async def handler(event):
            events.append(event)

        automation.bind({"fp2": fp})
        before = time.time_ns() // 1_000_000
        fp.update(n=1)
        await self.idle()
        self.assertEqual(events[0].time, fp.changed_at)
        self.assertGreaterEqual(events[0].time, before)

    async def test_last_seen_updated_by_same_value(self):
        fp = FakeDevice("fp2", {"connected": True, "n": 1})
        fp.update(n=1)
        changed, fp.last_seen = fp.changed_at, 0
        fp.update(n=1)  # valeur reçue identique : vue, sans changement
        self.assertGreater(fp.last_seen, 0)
        self.assertEqual(fp.changed_at, changed)
        fp.update(connected=False)
        fp.last_seen = 0
        fp.update(connected=False)  # injoignable : rien de reçu
        self.assertEqual(fp.last_seen, 0)

    async def test_to_filter_accepts_false(self):
        fp = FakeDevice("fp2", {"presence_0": True})
        events = []

        @automation.on_change("fp2.presence_0", to=False)
        async def handler(event):
            events.append(event.new)

        automation.bind({"fp2": fp})
        fp.update(presence_0=False)
        fp.update(presence_0=True)
        await self.idle()
        self.assertEqual(events, [False])

    async def test_whole_device_handler(self):
        fp = FakeDevice("fp2", {"a": 1})
        events = []

        @automation.on_change("fp2")
        async def handler(event):
            events.append(event)

        automation.bind({"fp2": fp})
        fp.update(a=2, b=3)
        await self.idle()
        self.assertEqual(len(events), 1)
        self.assertEqual(
            (events[0].attr, events[0].old, events[0].new), (None, {"a": 1}, {"a": 2, "b": 3})
        )
        self.assertEqual(events[0].changed, {"a", "b"})

    async def test_unreachable_is_not_a_change(self):
        fp = FakeDevice("fp2", {"connected": True, "presence_0": True})
        events = []

        @automation.on_change("fp2.presence_0")
        async def handler(event):
            events.append((event.old, event.new))

        automation.bind({"fp2": fp})
        fp._state = {"connected": False}  # coupure : l'attribut disparaît
        fp._notify()
        fp.update(connected=True, presence_0=True)  # retour, même valeur : rien
        await self.idle()
        self.assertEqual(events, [])
        fp._state = {"connected": False}
        fp._notify()
        fp.update(connected=True, presence_0=False)  # a changé pendant la coupure
        await self.idle()
        self.assertEqual(events, [(True, False)])

    async def test_events_run_in_order(self):
        fp = FakeDevice("fp2", {"n": 0})
        done = []

        @automation.on_change("fp2.n")
        async def handler(event):
            await asyncio.sleep(0.05 if event.new == 1 else 0)  # le premier est le plus lent
            done.append(event.new)

        automation.bind({"fp2": fp})
        fp.update(n=1)
        fp.update(n=2)
        await self.idle()
        self.assertEqual(done, [1, 2])

    async def test_unreachable_device_never_stops_the_script(self):
        fp = FakeDevice("fp2", {"n": 0})
        salon, cuisine = FakeDevice("salon"), FakeDevice("cuisine")
        results = []

        @automation.on_change("fp2.n")
        async def handler(event):
            results.append(await salon.send("on", event.new))
            results.append(await cuisine.send("on", event.new))

        automation.bind({"fp2": fp, "salon": salon, "cuisine": cuisine})
        salon.fail = DeviceError("injoignable")
        with self.assertLogs("core.base", "WARNING") as logs:
            fp.update(n=1)
            await self.idle()
        self.assertIn("injoignable", logs.output[0])
        self.assertEqual(results, [False, True])  # la suite du script a bien tourné
        self.assertEqual(cuisine.commands, [("on", 1)])
        salon.fail = None
        fp.update(n=2)
        await self.idle()
        self.assertEqual(salon.commands, [("on", 2)])

    async def test_invalid_command_is_not_swallowed(self):
        fp = FakeDevice("fp2", {"n": 0})
        bulb = FakeDevice("salon")
        bulb.fail = ValueError("on attend True/False")

        @automation.on_change("fp2.n")
        async def handler(event):
            await bulb.set("on", "oups")

        automation.bind({"fp2": fp, "salon": bulb})
        with self.assertLogs("core.automation", "WARNING") as logs:
            fp.update(n=1)
            await self.idle()
        self.assertIn("ValueError", logs.output[0])

    async def test_unexpected_error_is_logged_with_traceback(self):
        fp = FakeDevice("fp2", {"n": 0})

        @automation.on_change("fp2.n")
        async def handler(event):
            return {}["absent"]

        automation.bind({"fp2": fp})
        with self.assertLogs("core.base", "ERROR") as logs:
            fp.update(n=1)
            await self.idle()
        self.assertIn("erreur inattendue", logs.output[0])
        self.assertIn("KeyError", logs.output[0])

    async def test_unknown_device_is_reported_and_skipped(self):
        @automation.on_change("fantome.x")
        async def handler(event):
            raise AssertionError("ne doit pas être appelée")

        with self.assertLogs("core.automation", "ERROR") as logs:
            automation.bind({})
        self.assertIn("fantome", logs.output[0])

    def test_invalid_declarations(self):
        with self.assertRaises(ValueError):
            automation.on_change(".presence")
        with self.assertRaises(TypeError):

            @automation.on_change("fp2.x")
            def not_async(event):
                pass


class AttributeTest(AutomationTestCase):
    """Lecture et écriture simplifiées : fp2.illuminance, salon.on = True."""

    def test_read_is_live_state_none_when_unknown(self):
        fp = make_fp2()
        fp._connected = True
        fp._synced = True
        fp._values = {"illuminance": 42.5, "zone.cuisine": True, "zone.3": False}
        self.assertEqual(fp.illuminance, 42.5)
        self.assertTrue(fp.connected)
        self.assertTrue(fp.zone.cuisine)
        self.assertFalse(fp.zone[3])
        fp._connected = False  # injoignable : les attributs disparaissent
        fp._synced = False
        self.assertIsNone(fp.illuminance)
        self.assertFalse(fp.connected)
        self.assertIsNone(fp.zone.nexistepas)
        fp._connected = True
        fp._synced = True
        fp._values["illuminance"] = 10.0
        self.assertEqual(fp.illuminance, 10.0)  # suit l'état en direct

    def test_readonly_attribute_refuses_assignment(self):
        fp = make_fp2()
        with self.assertRaises(AttributeError):
            fp.illuminance = 5.0

    async def test_assignment_sends_command_without_waiting(self):
        bulb = FakeBulb("salon")
        bulb.on = True
        self.assertEqual(bulb.commands, [])  # pas encore parti : l'écriture ne bloque pas
        await self.idle()
        self.assertEqual(bulb.commands, [("on", True)])
        self.assertEqual(bulb.on, True)

    async def test_assignments_keep_order_per_device(self):
        bulb = FakeBulb("salon")
        bulb.delays = [0.05, 0]  # la première commande est la plus lente
        bulb.on = True
        bulb.bright = 80
        await self.idle()
        self.assertEqual(bulb.commands, [("on", True), ("bright", 80)])

    async def test_unreachable_device_is_logged_not_raised(self):
        bulb = FakeBulb("salon")
        bulb.fail = DeviceError("injoignable")
        with self.assertLogs("core.base", "WARNING") as logs:
            bulb.on = True
            await self.idle()
        self.assertIn("injoignable", logs.output[0])

    async def test_refused_command_is_a_warning(self):
        bulb = FakeBulb("salon")
        bulb.fail = ValueError("on attend True/False")
        with self.assertLogs("core.base", "WARNING") as logs:
            bulb.on = "oups"
            await self.idle()
        self.assertIn("refusée", logs.output[0])

    async def test_awaited_send_returns_success(self):
        bulb = FakeBulb("salon")
        self.assertTrue(await bulb.send("on", True))
        bulb.fail = DeviceError("injoignable")
        with self.assertLogs("core.base", "WARNING"):
            self.assertFalse(await bulb.send("on", False))
        bulb.fail = ValueError("refusé")
        with self.assertRaises(ValueError):  # erreur du script : elle remonte
            await bulb.send("on", "oups")

    async def test_script_style_from_the_docs(self):
        fp = FakeDevice("fp2", {"connected": True, "illuminance": 10.0, "zone.Global": False})
        salon, cuisine = FakeBulb("salon"), FakeBulb("cuisine")

        @automation.on_change("fp2.zone.Global", to=True)
        async def eclairage(event):
            if fp.state().get("illuminance", 100) < 50:
                salon.on = True
                cuisine.on = True

        automation.bind({"fp2": fp})
        fp.update(**{"zone.Global": True})
        await self.idle()
        self.assertEqual((salon.commands, cuisine.commands), ([("on", True)], [("on", True)]))
        fp._state = {"connected": False}  # injoignable : ne plante pas, ne fait rien
        fp._notify()
        fp.update(**{"connected": True, "illuminance": 80.0, "zone.Global": False})
        fp.update(**{"zone.Global": True})
        await self.idle()
        self.assertEqual(len(salon.commands), 1)  # assez de lumière : pas de nouvelle commande


class PresenceRuleTest(AutomationTestCase):
    """La règle d'automations/presence.py (configuration de test).

    Avec le vrai pilote FP2 et un faux appairage.
    """

    async def asyncSetUp(self):
        from automations import presence

        self.salon, self.cuisine = presence.salon, presence.cuisine  # vraies ampoules, sans réseau
        self.salon.configure("salon")  # normalement fait par load_devices()
        self.cuisine.configure("cuisine")
        self.commands = {"salon": [], "cuisine": []}
        self.fail: dict[str, Exception] = {}
        for dev in (self.salon, self.cuisine):
            patcher = mock.patch.object(dev, "set", self.fake_set(dev))
            patcher.start()
            self.addCleanup(patcher.stop)
        importlib.reload(presence)  # ré-enregistre la règle après le nettoyage de setUp
        self.fp = make_fp2(zonemapping={0: "Global"})
        automation.bind({"fp2": self.fp})
        self.pairing = FakePairing()
        task = asyncio.create_task(self.fp._serve(self.pairing))
        self.addAsyncCleanup(self.cancel, task)
        await until(lambda: self.fp._pairing is not None)
        await self.idle()

    def fake_set(self, dev):
        async def fake(name, value, transition=None):
            if dev.id in self.fail:
                raise self.fail[dev.id]
            self.commands[dev.id].append((name, value))

        return fake

    @staticmethod
    async def cancel(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_lights_follow_presence_0(self):
        # FP2 connecté, capteur 0 sans présence : les deux ampoules reçoivent « off »
        self.assertEqual(self.commands["salon"], [("on", False)])
        self.assertEqual(self.commands["cuisine"], [("on", False)])
        self.pairing.emit("event", {presence_key(0): {"value": 1}})
        await self.idle()
        self.assertEqual(self.commands["salon"][-1], ("on", True))
        self.assertEqual(self.commands["cuisine"][-1], ("on", True))
        self.pairing.emit("event", {presence_key(0): {"value": 0}})
        await self.idle()
        self.assertEqual(self.commands["salon"][-1], ("on", False))
        self.assertEqual(self.commands["cuisine"][-1], ("on", False))

    async def test_other_sensors_do_not_trigger(self):
        before = len(self.commands["salon"])
        self.pairing.emit("event", {presence_key(1): {"value": 0}})
        self.pairing.emit("event", {presence_key(1): {"value": 1}})
        await self.idle()
        self.assertEqual(len(self.commands["salon"]), before)

    async def test_fp2_disconnect_does_not_touch_lights(self):
        self.pairing.emit("event", {presence_key(0): {"value": 1}})
        await self.idle()
        before = {k: list(v) for k, v in self.commands.items()}
        self.pairing.emit("availability", False)
        await self.idle()
        self.assertEqual(self.commands, before)

    async def test_one_unreachable_bulb_does_not_block_the_other(self):
        self.fail["salon"] = DeviceError("injoignable")
        with self.assertLogs("core.base", "WARNING"):
            self.pairing.emit("event", {presence_key(0): {"value": 1}})
            await self.idle()
        self.assertEqual(self.commands["cuisine"][-1], ("on", True))


if __name__ == "__main__":
    unittest.main()
