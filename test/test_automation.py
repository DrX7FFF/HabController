"""Tests hors ligne : python -m unittest discover -s test -p "test_automation.py" -v"""

import asyncio
import importlib
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any, override
from unittest import mock

# configuration de test (test/config/), jamais celle de l'installation
os.environ["HABCTRL_CONFIG"] = str(Path(__file__).resolve().parent / "config")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_fp2 import FakePairing, make_fp2, presence_key, until

import drivers.all
from core import automation, base
from core.base import Device, DeviceError, State, Switchable, spawn_task


class FakeBulb(Switchable):
    """Ampoule simulée : attributs State, actions en méthodes, commandes relevées."""

    bright = State[int]()
    event_bright = base.EventSource[base.EventInt]()
    event_action = base.EventSource[base.EventAction]()

    def __init__(self, id):
        super().__init__()
        self.configure(id)
        self.commands: list[tuple] = []
        self.fail: Exception | None = None
        self.delays: list[float] = []  # attente simulée, consommée une commande à la fois

    def update(self, **values):
        """Réception simulée."""
        for name, value in values.items():
            match name:
                case "connected":
                    if not value:
                        self._reset()
                    Device.connected.update(self, value)
                case "on":
                    FakeBulb.on.update(self, value)
                case "bright":
                    FakeBulb.bright.update(self, value)

    def set_bright(self, value):
        self._background(f"set_bright({value})", lambda: self._command("bright", value))

    @override
    async def _switch(self, on, transition):
        await self._command("on", on)

    async def _command(self, name, value):
        if self.delays:
            await asyncio.sleep(self.delays.pop(0))
        if self.fail:
            raise self.fail
        self.commands.append((name, value))
        self.update(**{name: value})


class AutomationTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await automation.unload_automations()
        await base.cancel()

    async def idle(self):
        # Les tâches peuvent en lancer d'autres (commandes). Seulement celles en cours : voir
        # base.cancel(), une tâche finie peut rester dans tasks.
        while tasks := [task for task in base.tasks if not task.done()]:
            await asyncio.gather(*tasks, return_exceptions=True)


class DeviceTest(AutomationTestCase):
    async def test_same_value_is_not_a_change(self):
        bulb = FakeBulb("salon")
        bulb.update(connected=True, bright=1)
        changed = bulb.changed_at
        bulb.update(bright=1)
        self.assertEqual(bulb.changed_at, changed)

    async def test_state_broadcast_only_on_change(self):
        bulb, states = FakeBulb("salon"), []
        bulb.subscribe(states.append)
        bulb.update(connected=True, bright=1)
        bulb.update(bright=1)  # rien de changé : pas diffusé
        bulb.update(connected=False)  # bright disparaît : diffusé
        bulb.update(connected=True, bright=1)  # bright revient identique : diffusé
        self.assertEqual(
            states,
            [
                {"connected": True},
                {"connected": True, "bright": 1},
                {"connected": False},
                {"connected": True},
                {"connected": True, "bright": 1},
            ],
        )

    async def test_stop_with_finished_tasks_not_yet_removed(self):
        async def quick():
            pass

        spawn_task(quick(), "quick")
        await asyncio.sleep(0)  # la tâche est finie, son rappel _done pas encore passé
        self.assertTrue(all(task.done() for task in base.tasks))
        self.assertTrue(base.tasks)
        await base.cancel()  # ne doit pas tourner à vide


class AttributeTest(AutomationTestCase):
    """Attributs en lecture (fp2.illuminance), actions en méthodes (salon.switch(True))."""

    def test_read_is_live_state_none_when_unknown(self):
        fp = make_fp2(zonemapping={2: "cuisine"})
        fp._apply_values({"illuminance": 42.5, "zone.cuisine": True, "zone.3": False})
        fp._set_connected(True, synced=True)
        self.assertEqual(fp.illuminance, 42.5)
        self.assertTrue(fp.connected)
        self.assertTrue(fp.zones.cuisine)
        self.assertFalse(fp.zones[3])
        fp._set_connected(False)  # injoignable : les attributs valent None
        self.assertIsNone(fp.illuminance)
        self.assertFalse(fp.connected)
        self.assertIsNone(fp.zones.cuisine)
        fp._set_connected(True, synced=True)
        fp._apply_values({"illuminance": 10.0})
        self.assertEqual(fp.illuminance, 10.0)
        with self.assertRaises(AttributeError):
            _ = fp.zones.cuisinne  # faute de frappe : signalée par Python

    def test_readonly_attribute_refuses_assignment(self):
        fp = make_fp2()
        with self.assertRaises(AttributeError):
            fp.illuminance = 5.0

    def test_state_attribute_refuses_assignment(self):
        with self.assertRaises(AttributeError):
            FakeBulb("salon").on = True

    async def test_action_sends_command_without_waiting(self):
        bulb = FakeBulb("salon")
        bulb.switch(True)
        self.assertEqual(bulb.commands, [])  # pas encore parti : l'action ne bloque pas
        await self.idle()
        self.assertEqual(bulb.commands, [("on", True)])
        self.assertEqual(bulb.on, True)

    async def test_actions_keep_order_per_device(self):
        bulb = FakeBulb("salon")
        bulb.delays = [0.05, 0]  # la première commande est la plus lente
        bulb.switch(True)
        bulb.set_bright(80)
        await self.idle()
        self.assertEqual(bulb.commands, [("on", True), ("bright", 80)])

    async def test_unreachable_device_is_logged_not_raised(self):
        bulb = FakeBulb("salon")
        bulb.fail = DeviceError("injoignable")
        with self.assertLogs("core.base", "WARNING") as logs:
            bulb.switch(True)
            await self.idle()
        self.assertIn("injoignable", logs.output[0])

    async def test_refused_command_is_a_warning(self):
        bulb = FakeBulb("salon")
        with self.assertLogs("core.base", "WARNING") as logs:
            bulb.switch("oups")  # pyright: ignore[reportArgumentType] (refus testé)
            await self.idle()
        self.assertIn("refusée", logs.output[0])

    async def test_script_style_from_the_docs(self):
        fp, salon, cuisine = FakeBulb("fp2"), FakeBulb("salon"), FakeBulb("cuisine")
        fp.update(connected=True, bright=10, on=False)

        @fp.event_on
        async def eclairage(event):
            if event.new and (fp.bright or 100) < 50:
                salon.switch(True)
                cuisine.switch(True)

        fp.update(on=True)
        await self.idle()
        self.assertEqual((salon.commands, cuisine.commands), ([("on", True)], [("on", True)]))
        fp.update(connected=False)  # injoignable : ne plante pas, ne fait rien
        fp.update(connected=True, bright=80, on=False)
        fp.update(on=True)
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
        bulb = FakeBulb("lampe")

        def fake_import(module):  # chaque fichier s'abonne ; « b » échoue ensuite
            async def rule(event):
                pass

            rule.__module__ = module
            bulb.event_bright(rule)
            if module == "automations.b":
                raise NameError("erreur dans le script")
            return types.ModuleType(module)

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
            [func.__module__ for device, _, func in base.listeners() if device is bulb],
            ["automations.a", "automations.c"],
        )
        self.assertIn("b.py : fichier ignoré", logs.output[0])


class AppTest(AutomationTestCase):
    """KeepOnReload, @app_start, @app_reload : scripts écrits dans un dossier temporaire."""

    def load(self, files: dict[str, str], reload: bool) -> dict[str, Any]:
        def fake_import(name):  # exécute le fichier comme un vrai module de automations/
            mod = types.ModuleType(name)
            exec((Path(self.tmp) / f"{name.split('.')[1]}.py").read_text(), mod.__dict__)
            sys.modules[name] = mod
            return mod

        for name in [n for n in sys.modules if n.startswith("automations.")]:
            del sys.modules[name]  # comme forget_modules
        for path in Path(self.tmp).glob("*.py"):
            path.unlink()
        for name, text in files.items():
            (Path(self.tmp) / f"{name}.py").write_text(text)
        with (
            mock.patch.object(automation, "AUTOMATIONS_DIR", Path(self.tmp)),
            mock.patch.object(automation.importlib, "import_module", fake_import),
        ):
            automation.load_automations(reload)
        return {n: sys.modules.get(f"automations.{n}") for n in files}

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: [sys.modules.pop(n) for n in list(sys.modules)
                                 if n.startswith("automations.")])

    SCRIPT = "from core.automation import *\nstate: KeepOnReload = 'défaut'\nother = 'défaut'\n"

    async def test_kept_on_reload_only(self):
        mod = self.load({"a": self.SCRIPT}, reload=False)["a"]
        mod.state, mod.other = "changé", "changé"
        await automation.unload_automations()
        mod = self.load({"a": self.SCRIPT}, reload=True)["a"]
        self.assertEqual((mod.state, mod.other), ("changé", "défaut"))
        mod.state = "encore"
        await automation.unload_automations()
        mod = self.load({"a": self.SCRIPT}, reload=False)["a"]  # démarrage : valeur du script
        self.assertEqual(mod.state, "défaut")

    async def test_renamed_or_removed_forgotten(self):
        mod = self.load({"a": self.SCRIPT, "b": self.SCRIPT}, reload=False)["a"]
        mod.state = "changé"
        await automation.unload_automations()
        renamed = self.SCRIPT.replace("state:", "etat:")
        mod = self.load({"a": renamed}, reload=True)["a"]  # b supprimé
        self.assertEqual(mod.etat, "défaut")
        self.assertEqual(automation._kept, {})

    async def test_failing_file_keeps_its_values(self):
        mod = self.load({"a": self.SCRIPT}, reload=False)["a"]
        mod.state = "changé"
        await automation.unload_automations()
        with self.assertLogs("core.automation", "ERROR"):
            self.load({"a": self.SCRIPT + "raise NameError\n"}, reload=True)
        await automation.unload_automations()
        mod = self.load({"a": self.SCRIPT}, reload=True)["a"]
        self.assertEqual(mod.state, "changé")

    async def test_app_events(self):
        script = (
            "from core.automation import *\ncalls = []\n"
            "@app_start\nasync def s():\n    calls.append('start')\n"
            "@app_reload\nasync def r():\n    calls.append('reload')\n"
        )
        mod = self.load({"a": script}, reload=False)["a"]
        await automation.fire_app("start")
        await automation.fire_app("reload")
        self.assertEqual(mod.calls, ["start", "reload"])
        await automation.unload_automations()  # désabonnés
        await automation.fire_app("start")
        self.assertEqual(mod.calls, ["start", "reload"])


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
        self.failures: dict[str, Exception] = {}
        for dev in (self.salon, self.cuisine):
            patcher = mock.patch.object(dev, "_switch", self.fake_switch(dev))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fp = presence.fp2
        self.fp.configure("fp2")
        self.pairing = FakePairing()
        task = asyncio.create_task(self.fp._serve(self.pairing))
        self.addAsyncCleanup(self.cancel, task)
        await until(lambda: self.fp._pairing is not None)
        await self.idle()

    def fake_switch(self, dev):
        async def fake(on, _transition=None):
            if dev.id in self.failures:
                raise self.failures[dev.id]
            self.commands[dev.id].append(("on", on))

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
        self.failures["salon"] = DeviceError("injoignable")
        with self.assertLogs("core.base", "WARNING"):
            self.pairing.emit("event", {presence_key(0): {"value": 1}})
            await self.idle()
        self.assertEqual(self.commands["cuisine"][-1], ("on", True))


class DriverEventsTest(unittest.TestCase):
    def test_each_attribute_has_its_event(self):
        for name in drivers.all.__all__:
            cls = getattr(drivers.all, name)
            for attr in dir(cls):
                if attr in ("battery_powered", "rgb_capable", "last_clean"):  # sans événement
                    continue
                if isinstance(getattr(cls, attr), State):
                    with self.subTest(driver=name, attribute=attr):
                        self.assertIsInstance(
                            getattr(cls, f"event_{attr}", None), base.EventSource
                        )


EventBulb = FakeBulb


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

        bulb.update(connected=True, on=True)
        bulb.update(connected=False)  # injoignable : l'attribut disparaît de l'état
        bulb.update(connected=True, on=True)  # même valeur au retour : filtrée
        await self.idle()
        self.assertEqual(seen, [True])

    async def test_action(self):
        bulb, seen = EventBulb("bouton"), []

        @bulb.event_action
        async def action(event):
            seen.append((event.action, event.value, event.time))

        bulb._emit("single")
        bulb._emit("rotate", 45)
        await self.idle()
        self.assertEqual([s[:2] for s in seen], [("single", None), ("rotate", 45)])
        self.assertLessEqual(seen[0][2], seen[1][2])

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
        async def fails(_event):
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

        device._apply_values({"zone.Global": True, "illuminance": 12.0})
        device._set_connected(True, synced=True)
        device._apply_values({"zone.Global": False, "zone.3": True, "illuminance": 12.0})
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
        self.assertEqual(base.listeners(), [])

    async def test_unreachable_device_never_stops_the_script(self):
        fp = FakeBulb("fp2")
        salon, cuisine = FakeBulb("salon"), FakeBulb("cuisine")

        @fp.event_bright
        async def handler(_event):
            salon.switch(True)
            cuisine.switch(True)

        salon.fail = DeviceError("injoignable")
        with self.assertLogs("core.base", "WARNING") as logs:
            fp.update(bright=1)
            await self.idle()
        self.assertIn("injoignable", logs.output[0])
        self.assertEqual(cuisine.commands, [("on", True)])  # la suite du script a bien tourné

    async def test_invalid_command_is_logged(self):
        fp, bulb = FakeBulb("fp2"), FakeBulb("salon")

        @fp.event_bright
        async def handler(_event):
            bulb.switch("oups")  # pyright: ignore[reportArgumentType] (refus testé)

        with self.assertLogs("core.base", "WARNING") as logs:
            fp.update(bright=1)
            await self.idle()
        self.assertIn("refusée", logs.output[0])

    async def test_unexpected_error_is_logged_with_traceback(self):
        fp = FakeBulb("fp2")

        @fp.event_bright
        async def handler(_event):
            return {}["absent"]

        with self.assertLogs("core.base", "ERROR") as logs:
            fp.update(bright=1)
            await self.idle()
        self.assertIn("erreur inattendue", logs.output[0])
        self.assertIn("KeyError", logs.output[0])

    async def test_unload_cancels_running_and_keeps_device_commands(self):
        fp, bulb = FakeBulb("fp2"), FakeBulb("salon")
        bulb.delays = [0.05]
        started = asyncio.Event()

        @fp.event_bright
        async def slow(_event):
            bulb.switch(True)  # commande de l'équipement : va à son terme
            started.set()
            await asyncio.sleep(10)  # automatisation en cours : annulée

        fp.update(bright=1)
        await started.wait()
        await automation.unload_automations()
        await self.idle()
        self.assertEqual(bulb.commands, [("on", True)])

    async def test_check_reports_untracked_measure(self):
        bulb = EventBulb("lampe")
        bulb.measures = lambda: {"on": "disabled", "bright": "tracked"}

        async def rule(event: object):
            pass

        bulb.event_on(rule)
        bulb.event_bright(rule)
        bulb.event_action(rule)
        with self.assertLogs("core.automation", "WARNING") as logs:
            automation.check()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("lampe.event_on : non suivie (reports), jamais déclenchée", logs.output[0])

    def test_sync_function_refused(self):
        bulb = EventBulb("lampe")
        with self.assertRaises(TypeError):
            bulb.event_on(lambda _event: None)  # pyright: ignore[reportArgumentType] (refus testé)


if __name__ == "__main__":
    unittest.main()
