import asyncio
import importlib
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager, suppress
from functools import partial
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocketState
from pydantic import BaseModel, ConfigDict, ValidationError, validate_call

from core import CONFIG_DIR, ROOT, TEMP_DIR, datalog, git, pair, push, sync
from core.automation import check as check_automations
from core.automation import fire_app, load_automations, unload_automations
from core.base import cancel, describe, now_ms, spawn_task
from core.charts import downsample, load_charts
from core.devices import load_devices, summarize, wait_ready, write_summary
from core.firmware import updates
from core.log import collect_errors
from core.log import setup as setup_logging
from core.widgets import load_blocks

setup_logging()
log = logging.getLogger("habctrl")

CONFIG_PACKAGES = ("devices", "automations")  # paquets Python de la configuration
SUMMARY = TEMP_DIR / "devices.yaml"  # équipements et leur appareil physique
BOOT = now_ms()  # démarrage du programme : la page voit qu'il a redémarré
BUSY = "une autre opération d'administration est en cours (appairage, rechargement, firmware…)"


# Erreurs de configuration (ce qui est en erreur est ignoré), par partie rechargeable (voir
# core/sync.py) : un rechargement partiel ne remplace que celles de sa partie.
errors: dict[str, list[str]] = {}


def config_errors() -> list[str]:
    return [error for part in sync.ACTIONS if part in errors for error in errors[part]]


def read_devices() -> tuple[dict, list[str]]:
    with collect_errors() as errs:
        devs = load_devices()
    return devs, errs


def read_dashboard(devs: dict) -> tuple[list[dict], list[dict], list[str]]:
    """Blocs des écrans et graphiques."""
    with collect_errors() as errs:
        loaded = load_blocks(devs), load_charts()
    return *loaded, errs


SETTINGS = CONFIG_DIR / "habctrl.toml"
boot_settings = SETTINGS.read_bytes()  # modifié : redémarrage (voir sync.check_app)
loaded_files = sync.snapshot(CONFIG_DIR)  # avant la lecture : une modification pendant sera vue
loaded_at = time.time()  # dernier chargement de la configuration (voir core/sync.py)
devices, errors["devices"] = read_devices()
blocks, charts, errors["dashboard"] = read_dashboard(devices)
boot_commit: str | None = None  # commit de HabController en marche
history: datalog.Datalog | None = None
notifier = push.setup()  # notifications push (ntfy.sh), indépendantes des rechargements
pong: int | None = None  # réception (ms) de la réponse au dernier ping, None depuis le ping
ping_question: push.Question | None = None  # dernier ping, retiré du téléphone au suivant
pairings = pair.protocols()  # {protocole: classe Pairing}
admin_lock = asyncio.Lock()  # une opération d'administration à la fois (appairage, sauvegarde…)
server = None  # uvicorn.Server lancé par ./habctrl : arrêté pour redémarrer (voir restart())
restart_requested = False
QUEUE_SIZE = 1000  # messages en attente pour une page au-delà desquels elle est décrochée
clients: dict[asyncio.Queue, asyncio.Task] = {}  # file de messages d'une page -> son envoi
live: set[asyncio.Queue] = set()  # pages en Direct : reçoivent les enregistrements du datalog


def broadcast(message: dict, queues=clients) -> None:
    for queue in queues:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:  # page décrochée : son envoi s'arrête et sa connexion est fermée
            clients[queue].cancel()


def measures(dev) -> dict | None:
    """État de surveillance des mesures, None tant que l'équipement n'est pas prêt."""
    return dev.measures() if dev.ready() else None


def snapshot() -> list[dict]:
    return [
        {**b, "state": devices[b["device"]].state(), "measures": measures(devices[b["device"]])}
        for b in blocks
    ]


def pairing_list() -> list[dict]:
    return [
        {
            "protocol": name,
            "label": cls.LABEL or name,
            "duration": cls.DURATION,
            "max_duration": cls.MAX_DURATION,
            "cleanup": cls.CLEANUP,
        }
        for name, cls in sorted(pairings.items())
    ]


def init_message() -> dict:
    return {"type": "init", "boot": BOOT, "blocks": snapshot(), "charts": charts,
            "errors": config_errors(), "pairings": pairing_list(),
            "firmware": updates.list(), "pairing": pairing.state(),
            "push": push_state()}


def push_state() -> dict:
    return {"topic": notifier.topic, "pong": pong}


def set_pong(value: int | None) -> None:
    global pong
    pong = value
    broadcast({"type": "push", "push": push_state()})


updates.on_change = lambda: broadcast({"type": "firmware", "firmware": updates.list()})


def on_device_change(dev_id: str, state: dict) -> None:
    broadcast({"type": "state", "device": dev_id, "state": state})


def on_device_ready(dev) -> None:
    broadcast({"type": "measures", "device": dev.id, "measures": measures(dev)})


def start_automations(reload: bool = False) -> None:
    """Charge automations/ et les abonne aux équipements chargés (reload : voir
    load_automations)."""
    with collect_errors() as errs:
        load_automations(reload)
    errors["automations"] = errs


async def start_datalog() -> None:
    """Lit datalog.toml, abonne le datalog aux équipements chargés et le démarre."""
    global history
    with collect_errors() as errs:
        try:
            history = datalog.setup(devices)
        except Exception:  # datalog.toml incorrect
            log.exception("datalog : réglages incorrects, pas d'historique")
            history = None
        if history:
            history.subscribe(lambda points: broadcast({"type": "datalog", "points": points}, live))
            await history.start()
    errors["datalog"] = errs


def check_datalog() -> None:
    if history:
        history.check()


async def check_when_ready(check) -> None:
    """Contrôle des mesures (voir Device.check_measure) une fois les appareils connus : un
    appareil Zigbee ne sait ce qu'il fournit qu'une fois rattaché."""
    await wait_ready()
    check()


async def after_start() -> None:
    """Une fois les appareils connus : résumé des équipements, puis contrôle des mesures."""
    await summarize(devices, SUMMARY)
    check_automations()
    check_datalog()


async def start() -> None:
    """Abonne pages, datalog et automatisations aux équipements chargés, puis les démarre (le
    datalog d'abord : une automatisation peut enregistrer dès son chargement)."""
    for dev in devices.values():
        dev.subscribe(partial(on_device_change, dev.id))
        dev.subscribe_ready(partial(on_device_ready, dev))
    await start_datalog()
    start_automations()
    await fire_app("start")  # avant les équipements : aucun de leurs événements ne le précède
    with collect_errors() as errs:
        results = await asyncio.gather(
            *(d.start() for d in devices.values()), return_exceptions=True
        )
        for dev, result in zip(devices.values(), results, strict=True):
            if isinstance(result, Exception):
                log.error("%s : démarrage impossible", dev.id, exc_info=result)
    errors["devices"] += errs
    spawn_task(after_start(), "résumé des équipements")


async def stop() -> None:
    await unload_automations()
    await cancel()  # résumé, commandes et envois du datalog en cours
    if history:
        await history.stop()
    results = await asyncio.gather(*(d.stop() for d in devices.values()), return_exceptions=True)
    for dev, result in zip(devices.values(), results, strict=True):
        if isinstance(result, Exception):
            log.error("%s : arrêt en erreur", dev.id, exc_info=result)


def config_modules(packages=CONFIG_PACKAGES) -> dict:
    return {
        name: module
        for name, module in sys.modules.items()
        if name.partition(".")[0] in packages
    }


def forget_modules(packages=CONFIG_PACKAGES) -> dict:
    """Retire les modules de la configuration : le prochain import relit leurs fichiers.
    Renvoie les modules retirés."""
    previous = config_modules(packages)
    for name in previous:
        del sys.modules[name]
    importlib.invalidate_caches()
    return previous


async def reload_all() -> None:
    """Relit toute la configuration et redémarre équipements, automatisations et datalog ;
    erreur inattendue au chargement : levée, et l'ancienne configuration reste en marche."""
    global devices, blocks, charts
    previous = forget_modules()
    try:
        devs, devs_errors = read_devices()
        loaded = read_dashboard(devs)
    except Exception:
        forget_modules()
        sys.modules.update(previous)
        raise
    await stop()
    devices, errors["devices"] = devs, devs_errors
    blocks, charts, errors["dashboard"] = loaded
    await start()


async def reload_automations() -> None:
    """Recharge automations/ : équipements et datalog restent en marche (les automatisations
    importent les équipements en marche, devices/ n'est pas relu)."""
    await unload_automations()
    forget_modules(("automations",))
    start_automations(reload=True)
    await fire_app("reload")
    spawn_task(check_when_ready(check_automations), "contrôle des automatisations")


async def reload_datalog() -> None:
    """Relit datalog.toml : le nouveau datalog prend la relève, puis l'ancien finit ses envois."""
    old = history
    await start_datalog()
    spawn_task(check_when_ready(check_datalog), "contrôle du datalog")
    if old:
        await old.stop()


async def reload() -> list[str]:
    """Recharge les parties de la configuration modifiées depuis le dernier chargement (voir
    sync.reload_plan) ; renvoie le plan suivi. Ce qui est en erreur est ignoré (voir errors)."""
    global blocks, charts, loaded_files, loaded_at
    current = sync.snapshot(CONFIG_DIR)
    plan = sync.reload_plan(sync.changes(loaded_files, current))
    if "devices" in plan:
        await reload_all()
    if "automations" in plan:
        await reload_automations()
    if "datalog" in plan:
        await reload_datalog()
    if "dashboard" in plan:
        blocks, charts, errors["dashboard"] = read_dashboard(devices)
    loaded_files, loaded_at = current, time.time()
    if plan:
        broadcast(init_message())
        log.info("Configuration rechargée : %s", ", ".join(sync.ACTIONS[p] for p in plan))
    return plan


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global boot_commit
    await notifier.start()
    await start()
    if config_errors():
        log.warning("Configuration : %d erreur(s)", len(config_errors()))
    try:
        boot_commit = await git.head(ROOT)
    except git.GitError as err:
        log.warning("HabController : commit en marche inconnu (%s)", err)
    yield
    await stop()
    await notifier.stop()


app = FastAPI(lifespan=lifespan)


class CallMessage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["call"]
    device: str
    method: str  # méthode @actionweb du pilote (switch, set_brightness…)
    args: list[bool | int | str] = []


def apply_call(cmd: CallMessage) -> None:
    """Action demandée par une page : seulement les méthodes @actionweb du pilote, arguments
    vérifiés strictement d'après la signature de la méthode."""
    dev = devices.get(cmd.device)
    if dev is None:
        raise LookupError("équipement inconnu")
    if cmd.method not in dev.ACTIONS:
        raise ValueError(f"{cmd.device} n'a pas l'action « {cmd.method} »")
    validate_call(config=ConfigDict(strict=True))(getattr(dev, cmd.method))(*cmd.args)


# --- Synchronisations (core/sync.py) : contrôle (GET) puis synchro (POST) ----
# Contrôle : {"pending", "text"}. Synchro : {"text"} (résultat), "checks" ({nom : contrôle}
# des zones à rafraîchir), "restarting" (app). Les erreurs de la configuration rechargée arrivent
# aux pages par le message init du WebSocket.


async def check(name: str) -> dict:
    if name == "app":
        return await sync.check_app(ROOT, boot_commit, SETTINGS.read_bytes() != boot_settings)
    if name == "download":
        return await sync.check_download(CONFIG_DIR)
    if name == "load":
        return sync.check_load(CONFIG_DIR, loaded_files, loaded_at)
    return await sync.check_save(CONFIG_DIR)


SYNCS = {"app", "download", "load", "save"}


@app.get("/api/sync/{name}")
async def check_sync(name: str):
    if name not in SYNCS:
        raise HTTPException(status_code=404, detail="synchronisation inconnue")
    try:
        return await check(name)
    except git.GitError as err:
        raise HTTPException(status_code=502, detail=str(err)) from err


@app.post("/api/sync/{name}")
async def run_sync(name: str):
    if name not in SYNCS:
        raise HTTPException(status_code=404, detail="synchronisation inconnue")
    if name == "app" and server is None:
        raise HTTPException(status_code=501, detail="redémarrage impossible : lancer par ./habctrl")
    if admin_lock.locked():
        raise HTTPException(status_code=409, detail=BUSY)
    async with admin_lock:
        try:
            result = await {"app": update_code, "download": download_config,
                            "load": reload_config, "save": save_config}[name]()
            if not result.get("restarting"):
                after = {"download": ("download", "load")}.get(name, (name,))
                result["checks"] = {n: await check(n) for n in after}
        except (git.GitError, RuntimeError) as err:
            log.error("Synchronisation %s impossible : %s", name, err)
            raise HTTPException(status_code=502, detail=str(err)) from err
    return result


async def update_code() -> dict:
    """Met à jour HabController (git pull, dépendances si requirements.txt a changé), puis
    redémarre le programme, même sans changement."""
    files = await git.pull(ROOT)
    if "requirements.txt" in files:
        await install_requirements()
    log.info("HabController mis à jour (%d fichier(s) tiré(s))", len(files))
    asyncio.get_running_loop().call_later(0.5, restart)  # la réponse part avant l'arrêt
    text = f"{len(files)} fichier(s) mis à jour" if files else "Déjà à jour"
    return {"text": f"{text}, redémarrage…", "restarting": True}


async def download_config() -> dict:
    files = await git.pull(CONFIG_DIR, merge=True)  # modifié ici et ailleurs : fusionné
    log.info("Paramétrage téléchargé (%d fichier(s))", len(files))
    return {"text": f"{len(files)} fichier(s) téléchargé(s)." if files else "Déjà à jour."}


async def reload_config() -> dict:
    """Recharge les parties modifiées de la configuration."""
    try:
        plan = await reload()
    except Exception as err:
        log.exception("Rechargement de la configuration impossible")
        raise RuntimeError(f"configuration illisible : {type(err).__name__} : {err}") from err
    if not plan:
        return {"text": "Rien à recharger."}
    failed = f", {len(config_errors())} erreur(s)" if config_errors() else ""
    actions = ", ".join(sync.ACTIONS[part] for part in plan)
    return {"text": f"Rechargé : {actions} ({len(devices)} équipements{failed})."}


async def save_config() -> dict:
    """Sauvegarde HabConfig : commit de toutes les modifications puis push."""
    text = await git.backup(CONFIG_DIR)
    log.info("Sauvegarde de HabConfig : %s", text)
    return {"text": text[0].upper() + text[1:] + "."}


def restart() -> None:
    """Arrête proprement le serveur (équipements, automatisations, datalog), puis le programme
    se relance lui-même à la place (même processus : systemd n'y voit rien, voir __main__)."""
    global restart_requested
    log.info("Redémarrage demandé")
    restart_requested = True
    assert server is not None  # restart() n'est proposé que lancé par ./habctrl
    server.should_exit = True


async def install_requirements() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--quiet", "-r", str(ROOT / "requirements.txt"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode:
        lines = out.decode(errors="replace").strip().splitlines()
        raise RuntimeError("code mis à jour mais dépendances non installées (relancer "
                           f"install.sh) : {lines[-1] if lines else proc.returncode}")


@app.get("/api/history")
async def read_history(path: str, start: int, end: int):
    """Points [ms, valeur] d'un chemin du datalog entre start et end (ms)."""
    kinds = {s["path"]: s["type"] for c in charts for s in c["series"]}
    if path not in kinds:
        raise HTTPException(status_code=404, detail="série absente de charts.toml")
    if not 0 <= end - start <= 400 * 86_400_000:
        raise HTTPException(status_code=400, detail="période invalide")
    if history is None:
        raise HTTPException(status_code=503, detail="datalog non configuré")
    try:
        points = await history.read(path, start, end)
    except datalog.ERRORS as err:
        raise HTTPException(status_code=502, detail=f"datalog : {describe(err)}") from err
    if kinds[path] == "number":
        points = downsample(points)
    return {"path": path, "points": points}


# --- Ping : notification de test, son bouton Pong valide la boucle (envoi, réponse, réception)


@app.post("/api/push/ping", status_code=202)
async def ping():
    global ping_question
    set_pong(None)
    if ping_question:
        ping_question.cancel()
    ping_question = notifier.ask("🏓 Test de ping", {"Pong": lambda: set_pong(now_ms())})


# --- Firmwares (core/firmware.py) : installation de celui proposé pour un équipement --------
# Lancée en tâche de fond (jusqu'à une heure), sous le verrou d'administration : un rechargement
# l'interromprait. Avancement et résultat : par le WebSocket /ws (message firmware).
# Installé : le résumé des équipements est réécrit (nouvelle version).


async def install(dev_id: str) -> None:
    if await updates.install(dev_id):
        write_summary(devices, SUMMARY)


@app.post("/api/firmware/{dev_id}", status_code=202)
async def install_firmware(dev_id: str):
    if dev_id not in {o["device"] for o in updates.list()["offers"]}:
        raise HTTPException(status_code=404, detail="aucun firmware proposé pour cet équipement")
    if admin_lock.locked():
        raise HTTPException(status_code=409, detail=BUSY)
    await admin_lock.acquire()
    spawn_task(install(dev_id), f"firmware {dev_id}").add_done_callback(
        lambda _: admin_lock.release()
    )
    return {}


# --- WebSocket : événements dans les deux sens -------------------------------
# Tout ce que les pages affichent de l'état du serveur passe par ici (REST : actions et lectures
# à la demande seulement). Messages (champ type) du serveur vers la page :
# - init, à la connexion et à chaque rechargement de la configuration : boot (démarrage du
#   programme, ms), blocks (title, widget, device, state, measures : état de surveillance de
#   chaque mesure, null tant que l'équipement n'est pas prêt), charts, errors (configuration :
#   parties ignorées), pairings (protocol, label, duration, max_duration, cleanup), firmware,
#   pairing, push ;
# - state (device, state) : un équipement a changé ;
# - measures (device, measures) : un équipement devient prêt ou cesse de l'être ;
# - datalog (points : liste de chemin, ms, valeur) : enregistrement, aux pages en Direct ;
# - firmware (offers : device, current, version ; running ; progress en % ; last : résultat
#   de la dernière installation) : liste ou avancement changé ;
# - pairing (mode : none, scan, ask ; label : protocole ; log : lignes affichées ; ok : résultat,
#   null tant qu'il n'est pas terminé ; remaining : temps restant de la recherche, s ; question :
#   prompt, choices, secret) : appairage en cours ou dernier terminé, à chaque changement ;
# - push (topic, pong : réception de la réponse au ping, ms) : ping envoyé ou réponse reçue ;
# - error (device, detail) : commande refusée.
# De la page vers le serveur : call (device, method, args) : action de l'équipement ; live (on) :
# recevoir ou non les messages datalog ; pair (protocol, duration) : lance un appairage ;
# answer (value) : réponse à sa question ; pair_stop : l'arrête.


async def handle_message(message: dict, queue: asyncio.Queue) -> str | None:
    """Exécute une commande reçue ; renvoie un message d'erreur, ou None si tout va bien."""
    kind = message.get("type")
    try:
        if kind == "live":
            (live.add if message.get("on") else live.discard)(queue)
            return None
        if kind == "pair":
            return await start_pairing(message.get("protocol"), message.get("duration"))
        if kind == "answer":
            if isinstance(value := message.get("value"), str):
                pairing.answer(value)
            return None
        if kind == "pair_stop":
            stop_pairing()
            return None
        if kind != "call":
            return "type de message inconnu"
        apply_call(CallMessage.model_validate(message))
    except ValidationError:
        return "commande invalide"
    except (LookupError, ValueError, TypeError) as err:
        return str(err)
    return None


async def write_loop(ws: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        await ws.send_json(await queue.get())


async def read_loop(ws: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        try:
            message = json.loads(await ws.receive_text())
        except json.JSONDecodeError:
            queue.put_nowait({"type": "error", "detail": "message illisible"})
            continue
        if isinstance(message, dict):
            error = await handle_message(message, queue)
        else:
            error = "message invalide"
        if error:
            device = message.get("device") if isinstance(message, dict) else None
            queue.put_nowait({"type": "error", "device": device, "detail": error})
            if isinstance(device, str) and device in devices:
                # resynchronise l'affichage (curseur déplacé, commande refusée)
                state = devices[device].state()
                queue.put_nowait({"type": "state", "device": device, "state": state})


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
    queue.put_nowait(init_message())  # avant l'inscription : même ordre
    sender = clients[queue] = asyncio.create_task(write_loop(ws, queue))
    tasks = (sender, asyncio.create_task(read_loop(ws, queue)))
    try:
        # fin au premier des deux : page déconnectée, envoi arrêté (erreur ou file pleine)
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        del clients[queue]
        live.discard(queue)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if ws.client_state == WebSocketState.CONNECTED:
            with suppress(Exception):  # connexion déjà rompue
                await ws.close()


# --- Appairage : état du serveur, diffusé par le WebSocket /ws ----------------
# Un appairage à la fois (voir core/pair.py), en tâche de fond : il ne dépend d'aucune page.
# Message pairing (voir PairingDialog.state) ; lancé, répondu, arrêté par les messages pair,
# answer et pair_stop de /ws.


# Appareils appairés qu'aucun équipement n'utilise : liste (GET), retrait (DELETE).
# Après un retrait, le résumé des équipements est réécrit.
async def cleanup(protocol: str, action: str) -> list[str]:
    cls = pairings.get(protocol)
    if cls is None or not cls.CLEANUP:
        raise HTTPException(status_code=404, detail="protocole inconnu ou sans nettoyage")
    if admin_lock.locked():
        raise HTTPException(status_code=409, detail=BUSY)
    async with admin_lock:
        try:
            return await getattr(cls, action)()
        except cls.PROTOCOL.ERRORS as err:
            raise HTTPException(status_code=502, detail=str(err)) from err


@app.get("/api/pair/{protocol}/unused")
async def list_unused(protocol: str) -> list[str]:
    return await cleanup(protocol, "unused")


@app.delete("/api/pair/{protocol}/unused")
async def remove_unused(protocol: str) -> list[str]:
    removed = await cleanup(protocol, "remove_unused")
    if removed:
        write_summary(devices, SUMMARY)
    return removed


class PairingDialog:
    """Appairage en cours ou dernier terminé (core.pair.Dialog), diffusé à toutes les pages."""

    def __init__(self, label: str = ""):
        self.label = label
        self.mode = "none"  # none : sans appairage, scan : en cours, ask : attente d'une réponse
        self.lines: list[str] = []
        self.end = 0.0  # fin de la recherche (horloge monotone)
        self.question: dict | None = None
        self.ok: bool | None = None  # résultat, None : pas encore terminé
        self.answers: asyncio.Queue[str] = asyncio.Queue()

    def state(self) -> dict:
        return {
            "mode": self.mode, "label": self.label, "log": self.lines, "ok": self.ok,
            "remaining": max(0.0, self.end - time.monotonic()),
            "question": self.question,
        }

    def publish(self) -> None:
        broadcast({"type": "pairing", "pairing": self.state()})

    def say(self, text: str) -> None:
        self.lines.append(text)
        self.publish()

    def wait(self, seconds: int) -> None:
        self.end = time.monotonic() + seconds
        self.publish()

    async def ask(self, prompt: str, choices=None, secret: bool = False) -> str:
        while not self.answers.empty():  # réponse arrivée sans question : périmée
            self.answers.get_nowait()
        self.mode = "ask"
        self.question = {"prompt": prompt, "choices": choices or [], "secret": secret}
        self.publish()
        try:
            value = (await self.answers.get()).strip()
        finally:
            self.mode, self.question = "scan", None
        shown = "••••" if secret else dict(choices or []).get(value, value)
        self.say(f"> {prompt} : {shown}")
        return value

    def answer(self, value: str) -> None:
        if self.mode == "ask":
            self.answers.put_nowait(value)


pairing = PairingDialog()
pairing_task: asyncio.Task | None = None


async def run_pairing(cls, duration: int) -> None:
    global pairing
    dialog = pairing = PairingDialog(cls.LABEL)
    dialog.mode = "scan"
    dialog.publish()
    log.info("Appairage %s lancé (%s s)", cls.LABEL, duration)
    session = cls(dialog)
    ok = False
    try:
        try:
            ok = await session.run(duration)
        except Exception:
            log.exception("Appairage %s : erreur inattendue", cls.LABEL)
            dialog.say("Erreur inattendue, voir le journal.")
        if ok and (dev := devices.get(session.dev_id)):  # déjà en marche : relit son appairage
            try:
                await dev.stop()
                await dev.start()
                dialog.say(f"« {dev.id} » redémarré.")
            except Exception:
                log.exception("%s : redémarrage impossible", dev.id)
                dialog.say(f"« {dev.id} » : redémarrage impossible, voir le journal.")
        if ok:
            write_summary(devices, SUMMARY)
    finally:
        dialog.mode, dialog.question, dialog.end = "none", None, 0.0
        dialog.ok = ok
        dialog.publish()


async def start_pairing(protocol, duration) -> str | None:
    """Lance l'appairage demandé par une page ; renvoie un message d'erreur, ou None."""
    global pairing_task
    cls = pairings.get(protocol)
    if cls is None:
        return "protocole inconnu"
    if not isinstance(duration, int) or not 1 <= duration <= cls.MAX_DURATION:
        return f"durée entre 1 et {cls.MAX_DURATION} s"
    if admin_lock.locked():
        return BUSY
    await admin_lock.acquire()
    pairing_task = spawn_task(run_pairing(cls, duration), f"appairage {cls.LABEL}")
    pairing_task.add_done_callback(lambda _: admin_lock.release())
    return None


def stop_pairing() -> None:
    if pairing_task and not pairing_task.done():
        pairing.say("Arrêt demandé.")
        pairing_task.cancel()


# --- Pages web (web/) --------------------------------------------------------
# Page unique (web/index.html), qui affiche l'onglet de son adresse (voir web/app.js) ;
# styles, scripts et morceaux de page à la racine.

WEB_DIR = ROOT / "web"


@app.get("/", include_in_schema=False)
@app.get("/graphiques", include_in_schema=False)
@app.get("/administration", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


# Contenu de web/ servi à la racine : en dernier, les routes ci-dessus restent prioritaires.
app.mount("/", StaticFiles(directory=WEB_DIR), name="web")


if __name__ == "__main__":
    import uvicorn

    server = uvicorn.Server(  # journaux : core/log.py
        uvicorn.Config(app, host="0.0.0.0", port=8000, log_config=None)
    )
    server.run()
    if restart_requested:  # mise à jour : le nouveau code remplace ce processus
        os.execv(sys.executable, [sys.executable, *sys.argv])
