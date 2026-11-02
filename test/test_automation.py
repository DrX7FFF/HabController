"""Tests hors ligne : python -m unittest discover -s test -p "test_automation.py" -v"""

import asyncio
import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# configuration de test (test/config/), jamais celle de l'installation
os.environ["HABCTRL_CONFIG"] = str(Path(__file__).resolve().parent / "config")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_fp2 import FakePairing, make_fp2, presence_key, until  # noqa: E402

from core import automation, base  # noqa: E402
from core.base import Attribute, Device, DeviceError, spawn_task  # noqa: E402


class FakeDevice(Device):
    def __init__(self, id, state=None):
        super().__init__()
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
    async def asyncTearDown(self):
        await automation.unload_automations()
        await base.cancel_tasks()

    async def idle(self):
        # Les tâches peuvent en lancer d'autres (commandes). Seulement celles en cours : voir
        # base.cancel_tasks(), une tâche finie peut rester dans tasks.
        while tasks := [task for task in base.tasks if not task.done()]:
            await asyncio.gather(*tasks, return_exceptions=True)


class DeviceTest(AutomationTestCase):
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

    async def test_state_broadcast_only_on_change(self):
        fp, states = FakeDevice("fp2"), []
        fp.subscribe(states.append)
        fp.update(connected=True, n=1)
        fp.update(n=1)  # rien de changé : pas diffusé
        fp._state = {"connected": False}  # n disparaît : diffusé
        fp._notify()
        fp.update(connected=True, n=1)  # n revient identique : diffusé
        self.assertEqual(
            states,
            [{"connected": True, "n": 1}, {"connected": False}, {"connected": True, "n": 1}],
        )

    async def test_stop_with_finished_tasks_not_yet_removed(self):
        async def quick():
            pass

        spawn_task(quick(), "quick")
        await asyncio.sleep(0)  # la tâche est finie, son rappel _done pas encore passé
        self.assertTrue(all(task.done() for task in base.tasks))
        self.assertTrue(base.tasks)
        await base.cancel_tasks()  # ne doit pas tourner à vide


class AttributeTest(AutomationTestCase):
    """Lecture et écriture simplifiées : fp2.illuminance, salon.on = True."""

    def test_read_is_live_state_none_when_unknown(self):
        fp = make_fp2()
        fp._connected = True
        fp._synced = True
        fp._values = {"illuminance": 42.5, "zone.cuisine": True, "zone.3": False}
        self.assertEqual(fp.illuminance, 42.5)
        self.assertTrue(fp.connected)
        self.assertTrue(fp.zones.cuisine)
        self.assertFalse(fp.zones[3])
        fp._connected = False  # injoignable : les attributs disparaissent
        fp._synced = False
        self.assertIsNone(fp.illuminance)
        self.assertFalse(fp.connected)
        self.assertIsNone(fp.zones.nexistepas)
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
        fp = EventDevice("fp2", {"connected": True, "illuminance": 10.0, "n": 0})
        salon, cuisine = FakeBulb("salon"), FakeBulb("cuisine")

        @fp.event_n
        async def eclairage(event):
            if event.new and fp.state().get("illuminance", 100) < 50:
                salon.on = True
                cuisine.on = True

        fp.update(n=1)
        await self.idle()
        self.assertEqual((salon.commands, cuisine.commands), ([("on", True)], [("on", True)]))
        fp._state = {"connected": False}  # injoignable : ne plante pas, ne fait rien
        fp._notify()
        fp.update(connected=True, illuminance=80.0, n=0)
        fp.update(n=1)
        await self.idle()
        self.assertEqual(len(salon.commands), 1)  # assez de lumière : pas de nouvelle commande


class TimerTest(AutomationTestCase):
    async def test_fires_after_delay(self):
        fired = []
        timer = automation.Timer(0.05, lambda: fired.append(1))
        timer.start()
        self.assertTrue(timer.running)
        await self.idle()
        self.assertEqual(fired, [1])
        self.assertFalse(timer.running)

    async def test_restart_keeps_a_single_countdown(self):
        fired = []
        timer = automation.Timer(0.1, lambda: fired.append(time.monotonic()))
        start = time.monotonic()
        for _ in range(50):  # détection qui bagote : un seul décompte, relancé à chaque fois
            timer.start()
            await asyncio.sleep(0.002)
        last = time.monotonic()
        self.assertEqual(len([t for t in base.tasks if not t.done()]), 1)
        await self.idle()
        self.assertEqual(len(fired), 1)
        self.assertGreaterEqual(fired[0] - last, 0.09)
        self.assertGreater(fired[0] - start, 0.1)

    async def test_cancel(self):
        fired = []
        timer = automation.Timer(0.05, lambda: fired.append(1))
        timer.start()
        timer.cancel()
        timer.cancel()  # sans effet
        await asyncio.sleep(0.1)
        self.assertEqual(fired, [])
        self.assertFalse(timer.running)

    async def test_async_action_and_error(self):
        async def action():
            raise DeviceError("injoignable")

        timer = automation.Timer(0, action)
        with self.assertLogs("core.automation", "WARNING") as logs:
            timer.start()
            await self.idle()
        self.assertIn("injoignable", logs.output[0])

    async def test_cancelled_on_reload(self):
        fired = []
        timer = automation.Timer(0.05, lambda: fired.append(1))
        timer.start()
        await automation.unload_automations()
        await asyncio.sleep(0.1)
        self.assertEqual(fired, [])


class LoadTest(AutomationTestCase):
    async def test_failing_file_unloaded(self):
        bulb = EventDevice("lampe")

        def fake_import(module):  # chaque fichier s'abonne ; « b » échoue ensuite
            async def rule(event):
                pass

            rule.__module__ = module
            bulb.event_n(rule)
            if module == "automations.b":
                raise NameError("erreur dans le script")

        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.py", "b.py", "c.py"):
                (Path(tmp) / name).touch()
            with (
                mock.patch.object(automation, "AUTOMATIONS_DIR", Path(tmp)),
                mock.patch.object(automation.importlib, "import_module", fake_import),
                self.assertLogs("core.automation", "ERROR") as logs,
            ):
                automation.load_automations()
        self.assertEqual(
            [func.__module__ for func in bulb._evt_listeners["n"]],
            ["automations.a", "automations.c"],
        )
        self.assertIn("b.py : fichier ignoré", logs.output[0])


class PresenceRuleTest(AutomationTestCase):
    """La règle d'automations/presence.py (configuration de test).

    Avec le vrai pilote FP2 et un faux appairage.
    """

    async def asyncSetUp(self):
        # Import neuf à chaque test : la règle n'est enregistrée qu'une fois (un import suivi d'un
        # reload l'enregistrait deux fois au premier test, d'où des commandes en double).
        # Équipements neufs aussi : ils gardent les dernières valeurs vues.
        for module in ("automations.presence", "devices.salon", "devices.cuisine"):
            sys.modules.pop(module, None)
        presence = importlib.import_module("automations.presence")

        self.salon, self.cuisine = presence.salon, presence.cuisine  # vraies ampoules, sans réseau
        self.salon.configure("salon")  # normalement fait par load_devices()
        self.cuisine.configure("cuisine")
        self.commands = {"salon": [], "cuisine": []}
        self.fail: dict[str, Exception] = {}
        for dev in (self.salon, self.cuisine):
            patcher = mock.patch.object(dev, "set", self.fake_set(dev))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fp = presence.fp2
        self.fp.configure("fp2")
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


class DriverEventsTest(unittest.TestCase):
    def test_each_attribute_has_its_event(self):
        import drivers.all

        for name in drivers.all.__all__:
            cls = getattr(drivers.all, name)
            for attr in dir(cls):
                if isinstance(getattr(cls, attr), Attribute):
                    with self.subTest(driver=name, attribute=attr):
                        self.assertIsInstance(
                            getattr(cls, f"event_{attr}", None), base.EventSource
                        )


class EventDevice(FakeDevice):
    event_n = base.EventSource[base.EventInt]()


class EventBulb(FakeBulb):
    event_on = base.EventSource[base.EventBool]()
    event_bright = base.EventSource[base.EventInt]()
    event_action = base.EventSource[base.EventAction]()


class EventTest(AutomationTestCase):
    """Événements par attribut : @equipement.event_x."""

    async def test_first_value_then_changes_only(self):
        bulb, seen = EventBulb("lampe"), []

        @bulb.event_on
        async def on(event):
            seen.append((event.device, event.old, event.new))

        bulb.update(on=True, bright=10)
        bulb.update(on=True, bright=20)  # on identique : filtré
        bulb.update(on=False)
        await self.idle()
        self.assertEqual(seen, [(bulb, None, True), (bulb, True, False)])

    async def test_one_call_per_changed_attribute(self):
        bulb, seen = EventBulb("lampe"), []

        @bulb.event_on
        async def on(event):
            seen.append(("on", event.new))

        @bulb.event_bright
        async def bright(event):
            seen.append(("bright", event.new))

        bulb.update(on=True, bright=50)
        await self.idle()
        self.assertEqual(seen, [("on", True), ("bright", 50)])

    async def test_connected_event(self):
        bulb, seen = EventBulb("lampe"), []

        @bulb.event_connected
        async def connected(event):
            seen.append(event.new)

        bulb.update(connected=True)
        bulb.update(connected=False)
        await self.idle()
        self.assertEqual(seen, [True, False])

    async def test_value_kept_while_disconnected(self):
        bulb, seen = EventBulb("lampe"), []

        @bulb.event_on
        async def on(event):
            seen.append(event.new)

        bulb.update(on=True)
        bulb._state = {}  # injoignable : l'attribut disparaît de l'état
        bulb._notify()
        bulb.update(on=True)  # même valeur au retour : filtrée
        await self.idle()
        self.assertEqual(seen, [True])

    async def test_action(self):
        bulb, seen = EventBulb("bouton"), []

        @bulb.event_action
        async def action(event):
            seen.append((event.action, event.value, event.time))

        bulb._emit("single")
        first = bulb.last_seen
        bulb._emit("rotate", 45)
        await self.idle()
        self.assertEqual(seen, [("single", None, first), ("rotate", 45, bulb.last_seen)])

    async def test_device_events_run_in_order(self):
        bulb, seen = EventBulb("lampe"), []

        @bulb.event_bright
        async def slow(event):
            await asyncio.sleep(0.02 if event.new == 1 else 0)
            seen.append(event.new)

        @bulb.event_on
        async def fast(event):
            seen.append(event.new)

        bulb.update(bright=1)
        bulb.update(on=True)
        bulb.update(bright=2)
        await self.idle()
        self.assertEqual(seen, [1, True, 2])

    async def test_device_error_is_logged(self):
        bulb = EventBulb("lampe")

        @bulb.event_on
        async def fails(event):
            raise DeviceError("injoignable")

        with self.assertLogs("core.base", "WARNING"):
            bulb.update(on=True)
            await self.idle()

    async def test_zone(self):
        device, seen = make_fp2(zonemapping={0: "Global"}), []

        @device.event_zone
        async def zone(event):
            seen.append((event.zonename, event.old, event.new))

        @device.event_illuminance
        async def lux(event):
            seen.append(("lux", event.old, event.new))

        device._connected = device._synced = True
        device._values = {"zone.Global": True, "illuminance": 12.0}
        device._notify()
        device._values = {"zone.Global": False, "zone.3": True, "illuminance": 12.0}
        device._notify()
        await self.idle()
        self.assertEqual(
            seen,
            [
                ("Global", None, True),
                ("lux", None, 12.0),
                ("Global", True, False),
                ("3", None, True),
            ],
        )

    async def test_unload_removes_listeners(self):
        bulb, seen = EventBulb("lampe"), []

        @bulb.event_on
        async def on(event):
            seen.append(event.new)

        await automation.unload_automations()
        bulb.update(on=True)
        await self.idle()
        self.assertEqual(seen, [])
        self.assertEqual(bulb._evt_listeners, {"on": []})

    async def test_unreachable_device_never_stops_the_script(self):
        fp = EventDevice("fp2", {"n": 0})
        salon, cuisine = FakeDevice("salon"), FakeDevice("cuisine")
        results = []

        @fp.event_n
        async def handler(event):
            results.append(await salon.send("on", event.new))
            results.append(await cuisine.send("on", event.new))

        salon.fail = DeviceError("injoignable")
        with self.assertLogs("core.base", "WARNING") as logs:
            fp.update(n=1)
            await self.idle()
        self.assertIn("injoignable", logs.output[0])
        self.assertEqual(results, [False, True])  # la suite du script a bien tourné
        self.assertEqual(cuisine.commands, [("on", 1)])

    async def test_invalid_command_is_logged(self):
        fp, bulb = EventDevice("fp2"), FakeDevice("salon")
        bulb.fail = ValueError("on attend True/False")

        @fp.event_n
        async def handler(event):
            await bulb.set("on", "oups")

        with self.assertLogs("core.base", "WARNING") as logs:
            fp.update(n=1)
            await self.idle()
        self.assertIn("ValueError", logs.output[0])

    async def test_unexpected_error_is_logged_with_traceback(self):
        fp = EventDevice("fp2")

        @fp.event_n
        async def handler(event):
            return {}["absent"]

        with self.assertLogs("core.base", "ERROR") as logs:
            fp.update(n=1)
            await self.idle()
        self.assertIn("erreur inattendue", logs.output[0])
        self.assertIn("KeyError", logs.output[0])

    async def test_unload_cancels_running_and_keeps_device_commands(self):
        fp, bulb = EventDevice("fp2"), FakeBulb("salon")
        bulb.delays = [0.05]
        started = asyncio.Event()

        @fp.event_n
        async def slow(event):
            bulb.on = True  # commande de l'équipement : va à son terme
            started.set()
            await asyncio.sleep(10)  # automatisation en cours : annulée

        fp.update(n=1)
        await started.wait()
        await automation.unload_automations()
        await self.idle()
        self.assertEqual(bulb.commands, [("on", True)])

    async def test_check_reports_untracked_measure(self):
        bulb = EventBulb("lampe")
        bulb.measures = lambda: {"on": "disabled", "bright": "tracked"}

        @bulb.event_on
        @bulb.event_bright
        @bulb.event_action
        async def rule(event):
            pass

        with self.assertLogs("core.automation", "WARNING") as logs:
            automation.check()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("lampe.event_on : non suivie (reports), jamais déclenchée", logs.output[0])

    def test_sync_function_refused(self):
        bulb = EventBulb("lampe")
        with self.assertRaises(TypeError):
            bulb.event_on(lambda event: None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
