import asyncio
import json
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ValidationError

from core.base import DeviceError
from core.devices import load_devices
from core.widgets import load_blocks

devices = load_devices()
blocks = load_blocks(devices)
clients: set[asyncio.Queue] = set()  # une file de messages par page connectée


def broadcast(message: dict) -> None:
    for queue in clients:
        queue.put_nowait(message)


def snapshot() -> list[dict]:
    return [{**b, "state": devices[b["device"]].state()} for b in blocks]


def on_device_change(dev_id: str, state: dict) -> None:
    broadcast({"type": "state", "device": dev_id, "state": state})


for _dev in devices.values():
    _dev.subscribe(partial(on_device_change, _dev.id))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.gather(*(d.start() for d in devices.values()))
    yield
    await asyncio.gather(*(d.stop() for d in devices.values()))


app = FastAPI(lifespan=lifespan)


class SetCommand(BaseModel):
    name: str
    value: str | int
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


# --- WebSocket : événements dans les deux sens -------------------------------
# serveur -> page : {"type": "init", "blocks": [...]} puis {"type": "state", "device", "state"}
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


PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Domotique</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; }
  #banner { background: #fde8e8; border: 1px solid #b00; border-radius: .5rem;
            padding: .6rem 1rem; margin-bottom: 1rem; }
  #blocks { display: flex; flex-wrap: wrap; gap: 1rem; }
  .block { border: 2px solid #aaa; border-radius: 1rem; padding: 1rem 1.5rem;
           display: grid; gap: .8rem; min-width: 14rem; }
  .block.down { border-color: #b00; opacity: .6; }
  h2 { margin: 0; font-size: 1.1rem; }
  button { font-size: 1.2rem; padding: .8rem 1.5rem; border-radius: .8rem;
           border: 2px solid #888; cursor: pointer; }
  button.on { background: #ffd54a; border-color: #c9a100; }
  button.off { background: #ddd; }
  button.unreachable { background: #f5b5b5; border-color: #b00; }
  label { display: grid; gap: .3rem; }
</style>
</head>
<body>
<div id="banner" hidden></div>
<div id="blocks"></div>
<script>
const box = document.getElementById("blocks");
const banner = document.getElementById("banner");
const labels = { on: "allumé", off: "éteint", unreachable: "injoignable" };
let views = [];
let ws;

function send(device, name, value) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "set", device, name, value }));
  }
}

function show(text) {
  banner.textContent = text;
  banner.hidden = !text;
}

// Les contrôles ne sont pas écrasés par un événement pendant qu'on les manipule.
function slider(dev, name, text, min, max) {
  const wrap = document.createElement("label");
  const input = Object.assign(document.createElement("input"), { type: "range", min, max });
  let busy = false;
  input.oninput = () => { busy = true; };
  input.onchange = () => { send(dev, name, +input.value); busy = false; };
  wrap.append(text, input);
  return { el: wrap, set: s => { if (!busy && s[name] !== undefined) input.value = s[name]; } };
}

function colorPicker(dev) {
  const input = Object.assign(document.createElement("input"), { type: "color" });
  let busy = false;
  input.oninput = () => { busy = true; };
  input.onchange = () => { send(dev, "rgb", input.value); busy = false; };
  return { el: input, set: s => { if (!busy && s.rgb) input.value = s.rgb; } };
}

function build(b) {
  const el = document.createElement("div");
  el.className = "block";
  const title = document.createElement("h2");
  title.textContent = b.title;
  const power = document.createElement("button");
  power.onclick = () => send(b.device, "power", "toggle");
  const parts = [{ el: power, set: s => {
    power.className = s.power;
    power.textContent = labels[s.power] || s.power;
  } }];
  if (b.widget === "white") {
    parts.push(slider(b.device, "brightness", "Intensité", 1, 100));
    parts.push(slider(b.device, "color_temp", "Température", 1700, 6500));
  } else if (b.widget === "rgb") {
    parts.push(slider(b.device, "brightness", "Intensité", 1, 100));
    parts.push(colorPicker(b.device));
  }
  el.append(title, ...parts.map(p => p.el));
  return { el, device: b.device, update: s => {
    el.classList.toggle("down", s.power === "unreachable");
    parts.forEach(p => p.set(s));
  } };
}

function handle(m) {
  if (m.type === "init") {
    views = m.blocks.map(b => { const v = build(b); v.update(b.state); return v; });
    box.replaceChildren(...views.map(v => v.el));
    show("");
  } else if (m.type === "state") {
    views.filter(v => v.device === m.device).forEach(v => v.update(m.state));
  } else if (m.type === "error") {
    show(m.detail);
    setTimeout(() => show(""), 4000);
  }
}

function connect() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = e => handle(JSON.parse(e.data));
  ws.onclose = () => {
    views.forEach(v => v.update({ power: "unreachable" }));
    show("Connexion au serveur perdue…");
    setTimeout(connect, 2000);
  };
}

connect();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
