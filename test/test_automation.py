"""Tests hors ligne : python -m unittest discover -s test -p "test_automation.py" -v"""

import asyncio
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_fp2 import FakePairing, make_fp2, presence_key, until  # noqa: E402

from core import automation  # noqa: E402
from core.base import Device, DeviceError  # noqa: E402


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


class AutomationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        automation._handlers.clear()
        automation._equipments.clear()
        automation.devices.clear()

    async def asyncTearDown(self):
        await automation.stop_automations()
        automation._handlers.clear()

    async def idle(self):
        while automation._tasks:  # les tâches peuvent en lancer d'autres (commandes)
            await asyncio.gather(*list(automation._tasks), return_exceptions=True)


class OnChangeTest(AutomationTestCase):
    async def test_attribute_event(self):
        fp = FakeDevice("fp2", {"reachable": False})
        events = []

        @automation.on_change("fp2.presence_0")
        async def handler(event):
            events.append(event)

        automation.bind({"fp2": fp})
        fp.update(reachable=True, presence_0=False)  # premier état connu
        await self.idle()
        fp.update(presence_0=True)
        await self.idle()
        fp.update(presence_0=True)  # inchangé : rien
        fp.update(reachable=False)  # autre attribut : rien pour ce handler
        await self.idle()
        self.assertEqual(
            [(e.old, e.new) for e in events],
            [(None, False), (False, True)],
        )
        self.assertEqual(events[1].changed, {"presence_0"})
        self.assertEqual((events[1].device, events[1].attr), ("fp2", "presence_0"))

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
        fp = FakeDevice("fp2", {"reachable": True, "presence_0": True})
        events = []

        @automation.on_change("fp2.presence_0")
        async def handler(event):
            events.append((event.old, event.new))

        automation.bind({"fp2": fp})
        fp._state = {"reachable": False}  # coupure : l'attribut disparaît
        fp._notify()
        fp.update(reachable=True, presence_0=True)  # retour, même valeur : rien
        await self.idle()
        self.assertEqual(events, [])
        fp._state = {"reachable": False}
        fp._notify()
        fp.update(reachable=True, presence_0=False)  # a changé pendant la coupure
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
            results.append(await automation.devices["salon"].set("power", event.new))
            results.append(await automation.devices["cuisine"].set("power", event.new))

        automation.bind({"fp2": fp, "salon": salon, "cuisine": cuisine})
        salon.fail = DeviceError("injoignable")
        with self.assertLogs("core.automation", "WARNING") as logs:
            fp.update(n=1)
            await self.idle()
        self.assertIn("injoignable", logs.output[0])
        self.assertEqual(results, [False, True])  # la suite du script a bien tourné
        self.assertEqual(cuisine.commands, [("power", 1)])
        salon.fail = None
        fp.update(n=2)
        await self.idle()
        self.assertEqual(salon.commands, [("power", 2)])

    async def test_invalid_command_is_not_swallowed(self):
        fp = FakeDevice("fp2", {"n": 0})
        bulb = FakeDevice("salon")
        bulb.fail = ValueError("power attend on/off")

        @automation.on_change("fp2.n")
        async def handler(event):
            await automation.devices["salon"].set("power", "oups")

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
        with self.assertLogs("core.automation", "ERROR") as logs:
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


class EquipmentTest(AutomationTestCase):
    """Écriture simplifiée : fp2.illuminance, salon.power = "on"."""

    def test_import_by_name_is_the_same_object_as_devices(self):
        from core.automation import salon  # noqa: PLC0415

        automation.bind({"salon": FakeDevice("salon")})
        self.assertIs(salon, automation.devices["salon"])
        self.assertIs(salon, automation.salon)

    def test_private_and_dunder_names_are_not_equipments(self):
        for name in ("__nope__", "_nope"):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                getattr(automation, name)
        automation.bind({"salon": FakeDevice("salon")})
        with self.assertRaises(AttributeError):
            automation.salon._nope  # noqa: B018

    def test_read_is_live_state_none_when_unknown(self):
        fp = make_fp2()
        fp._connected = True
        fp._synced = True
        fp._values = {"illuminance": 42.5, "zone.cuisine": True, "zone.3": False}
        automation.bind({"fp2": fp})
        eq = automation.fp2
        self.assertEqual(eq.illuminance, 42.5)
        self.assertTrue(eq.reachable)
        self.assertTrue(eq.zone.cuisine)
        self.assertFalse(eq.zone[3])
        fp._connected = False  # injoignable : les attributs disparaissent
        fp._synced = False
        self.assertIsNone(eq.illuminance)
        self.assertFalse(eq.reachable)
        self.assertIsNone(eq.nexistepas)  # faute de frappe : None, sans erreur
        fp._connected = True
        fp._synced = True
        fp._values["illuminance"] = 10.0
        self.assertEqual(eq.illuminance, 10.0)  # suit l'état en direct

    async def test_assignment_sends_command_without_waiting(self):
        bulb = FakeDevice("salon")
        automation.bind({"salon": bulb})
        automation.salon.power = "on"
        self.assertEqual(bulb.commands, [])  # pas encore parti : l'écriture ne bloque pas
        await self.idle()
        self.assertEqual(bulb.commands, [("power", "on")])

    async def test_assignments_keep_order_per_device(self):
        bulb = FakeDevice("salon")
        bulb.delays = [0.05, 0]  # la première commande est la plus lente
        automation.bind({"salon": bulb})
        automation.salon.power = "on"
        automation.salon.bright = 80
        await self.idle()
        self.assertEqual(bulb.commands, [("power", "on"), ("bright", 80)])

    async def test_unreachable_device_is_logged_not_raised(self):
        bulb = FakeDevice("salon")
        bulb.fail = DeviceError("injoignable")
        automation.bind({"salon": bulb})
        with self.assertLogs("core.automation", "WARNING") as logs:
            automation.salon.power = "on"
            await self.idle()
        self.assertIn("injoignable", logs.output[0])

    async def test_refused_command_is_a_warning(self):
        bulb = FakeDevice("salon")
        bulb.fail = ValueError("power attend on/off")
        automation.bind({"salon": bulb})
        with self.assertLogs("core.automation", "WARNING") as logs:
            automation.salon.power = "oups"
            await self.idle()
        self.assertIn("refusée", logs.output[0])

    async def test_awaited_set_returns_success(self):
        bulb = FakeDevice("salon")
        automation.bind({"salon": bulb})
        self.assertTrue(await automation.salon.set("power", "on"))
        bulb.fail = DeviceError("injoignable")
        with self.assertLogs("core.automation", "WARNING"):
            self.assertFalse(await automation.salon.set("power", "off"))

    async def test_unknown_name_is_reported_at_bind(self):
        automation.fantome  # noqa: B018  (import d'un équipement qui n'existe pas)
        with self.assertLogs("core.automation", "ERROR") as logs:
            automation.bind({"salon": FakeDevice("salon")})
        self.assertIn("fantome", logs.output[0])
        with self.assertLogs("core.automation", "WARNING"):
            await automation.fantome.set("power", "on")
        self.assertIsNone(automation.fantome.power)

    async def test_script_style_from_the_docs(self):
        fp = FakeDevice("fp2", {"reachable": True, "illuminance": 10.0, "zone.Global": False})
        salon, cuisine = FakeDevice("salon"), FakeDevice("cuisine")

        @automation.on_change("fp2.zone.Global", to=True)
        async def eclairage(event):
            if automation.fp2.reachable and automation.fp2.illuminance < 50:
                automation.salon.power = "on"
                automation.cuisine.power = "on"

        automation.bind({"fp2": fp, "salon": salon, "cuisine": cuisine})
        fp.update(**{"zone.Global": True})
        await self.idle()
        self.assertEqual((salon.commands, cuisine.commands), ([("power", "on")], [("power", "on")]))
        fp._state = {"reachable": False}  # injoignable : ne plante pas, ne fait rien
        fp._notify()
        fp.update(**{"reachable": True, "illuminance": 80.0, "zone.Global": False})
        fp.update(**{"zone.Global": True})
        await self.idle()
        self.assertEqual(len(salon.commands), 1)  # assez de lumière : pas de nouvelle commande


class PresenceRuleTest(AutomationTestCase):
    """La règle réelle d'automations/presence.py, avec le vrai pilote FP2 et un faux appairage."""

    async def asyncSetUp(self):
        from automations import presence

        importlib.reload(presence)  # ré-enregistre la règle après le nettoyage de setUp
        self.salon = FakeDevice("salon", {"power": "off"})
        self.cuisine = FakeDevice("cuisine", {"power": "off"})
        self.fp = make_fp2(zonemapping={0: "Global"})
        automation.bind({"salon": self.salon, "cuisine": self.cuisine, "fp2": self.fp})
        self.pairing = FakePairing()
        task = asyncio.create_task(self.fp._serve(self.pairing))
        self.addAsyncCleanup(self.cancel, task)
        await until(lambda: self.fp._pairing is not None)
        await self.idle()

    @staticmethod
    async def cancel(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_lights_follow_presence_0(self):
        # FP2 connecté, capteur 0 sans présence : les deux ampoules reçoivent « off »
        self.assertEqual(self.salon.commands, [("power", "off")])
        self.assertEqual(self.cuisine.commands, [("power", "off")])
        self.pairing.emit("event", {presence_key(0): {"value": 1}})
        await self.idle()
        self.assertEqual(self.salon.commands[-1], ("power", "on"))
        self.assertEqual(self.cuisine.commands[-1], ("power", "on"))
        self.pairing.emit("event", {presence_key(0): {"value": 0}})
        await self.idle()
        self.assertEqual(self.salon.commands[-1], ("power", "off"))
        self.assertEqual(self.cuisine.commands[-1], ("power", "off"))

    async def test_other_sensors_do_not_trigger(self):
        before = len(self.salon.commands)
        self.pairing.emit("event", {presence_key(1): {"value": 0}})
        self.pairing.emit("event", {presence_key(1): {"value": 1}})
        await self.idle()
        self.assertEqual(len(self.salon.commands), before)

    async def test_fp2_disconnect_does_not_touch_lights(self):
        self.pairing.emit("event", {presence_key(0): {"value": 1}})
        await self.idle()
        before = (list(self.salon.commands), list(self.cuisine.commands))
        self.pairing.emit("availability", False)
        await self.idle()
        self.assertEqual((self.salon.commands, self.cuisine.commands), before)

    async def test_one_unreachable_bulb_does_not_block_the_other(self):
        self.salon.fail = DeviceError("injoignable")
        with self.assertLogs("core.automation", "WARNING"):
            self.pairing.emit("event", {presence_key(0): {"value": 1}})
            await self.idle()
        self.assertEqual(self.cuisine.commands[-1], ("power", "on"))


if __name__ == "__main__":
    unittest.main()
