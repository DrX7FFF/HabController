import asyncio
import importlib
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocketState
from pydantic import BaseModel, ValidationError

from core import CONFIG_DIR, ROOT, TEMP_DIR, datalog, git, pair, sync
from core.automation import load_automations, stop_automations
from core.base import DeviceError
from core.charts import downsample, load_charts
from core.devices import load_devices, summarize
from core.log import collect_errors
from core.log import setup as setup_logging
from core.notifications import Notifications
from core.widgets import load_blocks

setup_logging()
log = logging.getLogger("habctrl")

CONFIG_PACKAGES = ("devices", "automations")  # paquets Python de la configuration
BOOT = int(time.time() * 1000)  # démarrage du programme : la page voit qu'il a redémarré
BUSY = "une autre opération d'administration est en cours (appairage, rechargement…)"
notifications = Notifications()  # en attente : pastille sur l'onglet Administration


def load_config() -> tuple[dict, list[dict], list[dict], list[str]]:
    """Équipements, blocs des écrans et graphiques, et les erreurs de configuration rencontrées
    (ce qui est en erreur est ignoré)."""
    with collect_errors() as errors:
        devs = load_devices()
        loaded = devs, load_blocks(devs), load_charts()
    return *loaded, errors


devices, blocks, charts, config_errors = load_config()
loaded_at = time.time()  # dernier chargement de la configuration (voir core/sync.py)
boot_commit: str | None = None  # commit de HabController en marche
history: datalog.Datalog | None = None
summary: asyncio.Task | None = None
pairings = pair.protocols()  # {protocole: classe Pairing}
admin_lock = asyncio.Lock()  # une opération d'administration à la fois (appairage, sauvegarde…)
server = None  # uvicorn.Server lancé par ./habctrl : arrêté pour redémarrer (voir restart())
restart_requested = False
clients: set[asyncio.Queue] = set()  # une file de messages par page connectée


def broadcast(message: dict) -> None:
    for queue in clients:
        queue.put_nowait(message)


def snapshot() -> list[dict]:
    return [{**b, "state": devices[b["device"]].state()} for b in blocks]


def init_message() -> dict:
    return {"type": "init", "blocks": snapshot(), "errors": config_errors}


def on_device_change(dev_id: str, state: dict) -> None:
    broadcast({"type": "state", "device": dev_id, "state": state})


async def start() -> list[str]:
    """Abonne automatisations et datalog aux équipements chargés, puis les démarre. Renvoie les
    erreurs de configuration rencontrées (ce qui est en erreur est ignoré)."""
    global history, summary
    with collect_errors() as errors:
        for dev in devices.values():
            dev.subscribe(partial(on_device_change, dev.id))
        load_automations(devices)
        try:
            history = datalog.setup(devices)
        except Exception as err:  # [datalog] de habctrl.toml incorrect
            log.error("datalog : réglages incorrects (%s), pas d'historique", datalog.describe(err))
            history = None
        if history:
            history.subscribe(lambda points: broadcast({"type": "datalog", "points": points}))
            await history.start()
        results = await asyncio.gather(
            *(d.start() for d in devices.values()), return_exceptions=True
        )
        for dev, result in zip(devices.values(), results):
            if isinstance(result, Exception):
                log.error("%s : démarrage impossible", dev.id, exc_info=result)
    summary = asyncio.create_task(summarize(devices, TEMP_DIR / "equipements.txt"))
    return errors


async def stop() -> None:
    if summary:
        summary.cancel()
    await stop_automations()  # annule aussi les envois du datalog en cours
    if history:
        await history.stop()
    results = await asyncio.gather(*(d.stop() for d in devices.values()), return_exceptions=True)
    for dev, result in zip(devices.values(), results):
        if isinstance(result, Exception):
            log.error("%s : arrêt en erreur", dev.id, exc_info=result)


def config_modules() -> dict:
    return {
        name: module
        for name, module in sys.modules.items()
        if name.partition(".")[0] in CONFIG_PACKAGES
    }


async def reload() -> None:
    """Relit toute la configuration et redémarre équipements, automatisations et datalog. Ce qui
    est en erreur est ignoré (voir config_errors) ; erreur inattendue au chargement : levée, et
    l'ancienne configuration reste en marche."""
    global devices, blocks, charts, config_errors, loaded_at
    previous = config_modules()
    for name in previous:
        del sys.modules[name]
    importlib.invalidate_caches()
    try:
        loaded = load_config()
    except Exception:
        for name in config_modules():
            del sys.modules[name]
        sys.modules.update(previous)
        raise
    await stop()
    devices, blocks, charts, errors = loaded
    loaded_at = time.time()
    config_errors = errors + await start()
    broadcast(init_message())
    log.info("Configuration rechargée")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config_errors, boot_commit
    config_errors = config_errors + await start()
    if config_errors:
        log.warning("Configuration : %d erreur(s)", len(config_errors))
    try:
        boot_commit = await git.head(ROOT)
    except git.GitError as err:
        log.warning("HabController : commit en marche inconnu (%s)", err)
    yield
    await stop()


app = FastAPI(lifespan=lifespan)


class SetCommand(BaseModel):
    name: str
    value: bool | str | int  # sans bool, un true JSON arriverait en "1"
    transition: int | None = None  # ms, sinon valeur par défaut de l'équipement


class SetMessage(SetCommand):
    device: str


async def apply_command(dev_id: str, cmd: SetCommand) -> None:
    dev = devices.get(dev_id)
    if dev is None:
        raise LookupError("équipement inconnu")
    await dev.set(cmd.name, cmd.value, cmd.transition)


# --- REST (utile pour les tests : curl, /docs) -------------------------------


@app.get("/api/blocks")
async def list_blocks():
    return snapshot()


@app.post("/api/devices/{dev_id}/set")
async def set_device(dev_id: str, cmd: SetCommand):
    try:
        await apply_command(dev_id, cmd)
    except LookupError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except DeviceError as err:
        raise HTTPException(status_code=502, detail=str(err)) from err
    return devices[dev_id].state()


@app.get("/api/config")
async def config_status():
    """Nombre d'équipements chargés, erreurs de la configuration (parties ignorées) et date du
    démarrage du programme (ms)."""
    return {"devices": len(devices), "errors": config_errors, "boot": BOOT}


# --- Notifications (core/notifications.py) ----------------------------------


@app.get("/api/notifications")
async def list_notifications():
    """Notifications en attente : [{id, text, level, since (ms)}], les plus anciennes d'abord."""
    return notifications.list()


# --- Synchronisations (core/sync.py) : contrôle (GET) puis synchro (POST) ----
# Contrôle : {"pending", "text"}. Synchro : {"text"} (résultat), "checks" ({nom : contrôle}
# des zones à rafraîchir), "restarting" (app), "errors" (load : erreurs de la configuration).


async def check(name: str) -> dict:
    if name == "app":
        return await sync.check_app(ROOT, boot_commit)
    if name == "download":
        return await sync.check_download(CONFIG_DIR)
    if name == "load":
        return sync.check_load(CONFIG_DIR, loaded_at)
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
    redémarre le programme si le code a changé depuis son démarrage."""
    files = await git.pull(ROOT)
    if "requirements.txt" in files:
        await install_requirements()
    if await git.head(ROOT) == boot_commit:
        return {"text": "Déjà à jour."}
    log.info("HabController mis à jour (%d fichier(s) tiré(s))", len(files))
    asyncio.get_running_loop().call_later(0.5, restart)  # la réponse part avant l'arrêt
    return {"text": f"{len(files)} fichier(s) mis à jour, redémarrage…", "restarting": True}


async def download_config() -> dict:
    files = await git.pull(CONFIG_DIR, merge=True)  # modifié ici et ailleurs : fusionné
    log.info("Paramétrage téléchargé (%d fichier(s))", len(files))
    return {"text": f"{len(files)} fichier(s) téléchargé(s)." if files else "Déjà à jour."}


async def reload_config() -> dict:
    """Recharge toute la configuration (équipements, automatisations, écrans, graphiques,
    datalog) sans redémarrer le programme."""
    try:
        await reload()
    except Exception as err:
        log.exception("Rechargement de la configuration impossible")
        raise RuntimeError(f"configuration illisible : {type(err).__name__} : {err}") from err
    failed = f", {len(config_errors)} erreur(s)" if config_errors else ""
    return {"text": f"Configuration rechargée ({len(devices)} équipements{failed}).",
            "errors": config_errors}


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


@app.get("/api/charts")
async def list_charts():
    return charts


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
    except Exception as err:  # réseau, clé, droits
        raise HTTPException(status_code=502, detail=f"datalog : {datalog.describe(err)}") from err
    if kinds[path] == "number":
        points = downsample(points)
    return {"path": path, "points": points}


# --- WebSocket : événements dans les deux sens -------------------------------
# serveur -> page : {"type": "init", "blocks": [...], "errors": [...]} puis {"type": "state", "device", "state"},
#                   et {"type": "datalog", "points": [[chemin, ms, valeur], ...]} à chaque
#                   enregistrement du datalog (graphiques en direct)
#                   et {"type": "error", "device", "detail"} pour une commande refusée
# page -> serveur : {"type": "set", "device", "name", "value", "transition"?}


async def handle_message(message: dict) -> str | None:
    """Exécute une commande reçue ; renvoie un message d'erreur, ou None si tout va bien."""
    try:
        if message.get("type") != "set":
            return "type de message inconnu"
        await apply_command(message.get("device", ""), SetMessage.model_validate(message))
    except ValidationError:
        return "commande invalide"
    except (LookupError, ValueError, DeviceError) as err:
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
        error = await handle_message(message) if isinstance(message, dict) else "message invalide"
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
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(init_message())  # avant l'inscription : même ordre
    clients.add(queue)
    sender = asyncio.create_task(write_loop(ws, queue))
    sender.add_done_callback(lambda t: t.cancelled() or t.exception())  # évite un avertissement
    try:
        await read_loop(ws, queue)  # ne rend la main qu'à la déconnexion
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(queue)
        sender.cancel()


# --- Appairage : WebSocket /ws/pair?protocol=…&duration=… -------------------
# Un dialogue par appairage (voir core/pair.py), interrompu si la page se ferme.
# serveur -> page : {"type": "say", "text"}, {"type": "ask", "prompt", "choices", "secret"},
#                   {"type": "wait", "seconds"} (barre de progression ; 0 : masquée),
#                   puis {"type": "end", "ok"}
# page -> serveur : {"type": "answer", "value"}


@app.get("/api/pair")
async def list_pairings():
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


# Appareils appairés qu'aucun équipement n'utilise : liste (GET), retrait (DELETE).
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
    return await cleanup(protocol, "remove_unused")


class PairingDialog:
    """Dialogue de l'appairage (core.pair.Dialog) avec la page, par le WebSocket."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.outbox: asyncio.Queue = asyncio.Queue()  # None : fin de l'envoi
        self.answers: asyncio.Queue = asyncio.Queue()

    def say(self, text: str) -> None:
        self.outbox.put_nowait({"type": "say", "text": text})

    def wait(self, seconds: int) -> None:
        self.outbox.put_nowait({"type": "wait", "seconds": seconds})

    async def ask(self, prompt: str, choices=None, secret: bool = False) -> str:
        while not self.answers.empty():  # réponse arrivée sans question : périmée
            self.answers.get_nowait()
        self.outbox.put_nowait(
            {"type": "ask", "prompt": prompt, "choices": choices or [], "secret": secret}
        )
        return (await self.answers.get()).strip()

    async def send_loop(self) -> None:
        while (message := await self.outbox.get()) is not None:
            await self.ws.send_json(message)

    async def read_loop(self) -> None:
        """Rend la main à la déconnexion de la page."""
        while True:
            try:
                message = json.loads(await self.ws.receive_text())
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and isinstance(message.get("value"), str):
                self.answers.put_nowait(message["value"])


async def run_pairing(dialog: PairingDialog, cls, duration: int) -> None:
    pairing = asyncio.create_task(cls(dialog).run(duration))
    reader = asyncio.create_task(dialog.read_loop())
    sender = asyncio.create_task(dialog.send_loop())
    try:
        await asyncio.wait({pairing, reader}, return_when=asyncio.FIRST_COMPLETED)
        if not pairing.done():  # page fermée : on abandonne (la session se referme)
            return
        try:
            ok = pairing.result()
        except Exception:
            log.exception("Appairage %s : erreur inattendue", cls.LABEL)
            dialog.say("Erreur inattendue, voir le journal.")
            ok = False
        dialog.outbox.put_nowait({"type": "end", "ok": ok})
        dialog.outbox.put_nowait(None)
        await asyncio.wait({sender})  # tout est envoyé (ou la page est partie)
    finally:
        for task in (pairing, reader, sender):
            task.cancel()
        await asyncio.gather(pairing, reader, sender, return_exceptions=True)


@app.websocket("/ws/pair")
async def pair_socket(ws: WebSocket, protocol: str = "", duration: int = 0):
    await ws.accept()
    cls = pairings.get(protocol)
    if cls is None or not 1 <= duration <= cls.MAX_DURATION:
        error = "protocole inconnu" if cls is None else f"durée entre 1 et {cls.MAX_DURATION} s"
    elif admin_lock.locked():
        error = BUSY
    else:
        async with admin_lock:
            log.info("Appairage %s lancé (%s s)", cls.LABEL, duration)
            await run_pairing(PairingDialog(ws), cls, duration)
        if ws.client_state == WebSocketState.CONNECTED:
            await ws.close()
        return
    await ws.send_json({"type": "say", "text": f"Erreur : {error}."})
    await ws.send_json({"type": "end", "ok": False})
    await ws.close()


# --- Pages web (web/) --------------------------------------------------------
# Une page par onglet, servie à son adresse ; styles, scripts et bibliothèques sous /web/.

WEB_DIR = ROOT / "web"
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/graphiques", include_in_schema=False)
def charts_page():
    return FileResponse(WEB_DIR / "charts.html")


@app.get("/administration", include_in_schema=False)
def admin_page():
    return FileResponse(WEB_DIR / "admin.html")


if __name__ == "__main__":
    import uvicorn

    server = uvicorn.Server(  # journaux : core/log.py
        uvicorn.Config(app, host="0.0.0.0", port=8000, log_config=None)
    )
    server.run()
    if restart_requested:  # mise à jour : le nouveau code remplace ce processus
        os.execv(sys.executable, [sys.executable, *sys.argv])
