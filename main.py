import asyncio
import json
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from core import ROOT, TEMP_DIR, check_config, datalog
from core.automation import load_automations, stop_automations
from core.base import DeviceError
from core.charts import downsample, load_charts
from core.devices import load_devices, write_summary
from core.history_mock import mock_history
from core.log import setup as setup_logging
from core.widgets import load_blocks

check_config()
setup_logging()
devices = load_devices()
blocks = load_blocks(devices)
charts = load_charts()
clients: set[asyncio.Queue] = set()  # une file de messages par page connectée


def broadcast(message: dict) -> None:
    for queue in clients:
        queue.put_nowait(message)


def snapshot() -> list[dict]:
    return [{**b, "state": devices[b["device"]].state()} for b in blocks]


def on_device_change(dev_id: str, state: dict) -> None:
    broadcast({"type": "state", "device": dev_id, "state": state, "at": devices[dev_id].changed_at})


for _dev in devices.values():
    _dev.subscribe(partial(on_device_change, _dev.id))
load_automations(devices)
history = datalog.setup(devices)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if history:
        await history.start()
    await asyncio.gather(*(d.start() for d in devices.values()))
    write_summary(devices, TEMP_DIR / "equipements.txt")
    yield
    await stop_automations()  # annule aussi les envois du datalog en cours
    if history:
        await history.stop()
    await asyncio.gather(*(d.stop() for d in devices.values()))


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


@app.get("/api/charts")
async def list_charts():
    return charts


@app.get("/api/history")
async def read_history(path: str, start: int, end: int):
    """Points [ms, valeur] d'un chemin du datalog entre start et end (ms).
    Simulés si le datalog n'est pas configuré (configuration de test)."""
    kinds = {s["path"]: s["type"] for c in charts for s in c["series"]}
    if path not in kinds:
        raise HTTPException(status_code=404, detail="série absente de charts.toml")
    if not 0 <= end - start <= 400 * 86_400_000:
        raise HTTPException(status_code=400, detail="période invalide")
    if history is None:
        points = mock_history(path, kinds[path], start, end)
    else:
        try:
            points = await history.read(path, start, end)
        except Exception as err:  # réseau, clé, droits
            raise HTTPException(status_code=502, detail=f"datalog : {datalog.describe(err)}") from err
    if kinds[path] == "number":
        points = downsample(points)
    return {"path": path, "mock": history is None, "points": points}


# --- WebSocket : événements dans les deux sens -------------------------------
# serveur -> page : {"type": "init", "blocks": [...]} puis {"type": "state", "device", "state",
#                   "at"} (at : horodatage du changement en ms, sert aux graphiques en direct)
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
    queue.put_nowait({"type": "init", "blocks": snapshot()})  # avant l'inscription : même ordre
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
