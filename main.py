from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from devices import load_devices
from drivers.base import DeviceError
from widgets import load_blocks

devices = load_devices()
blocks = load_blocks(devices)
app = FastAPI()


class Command(BaseModel):
    name: str
    value: str | int
    transition: int | None = None  # ms, sinon valeur par défaut de l'équipement


@app.get("/api/blocks")
def list_blocks():
    ids = sorted({b["device"] for b in blocks})
    with ThreadPoolExecutor() as pool:  # états lus en parallèle
        states = dict(zip(ids, pool.map(lambda i: devices[i].state(), ids), strict=True))
    return [{**b, "state": states[b["device"]]} for b in blocks]


@app.post("/api/devices/{dev_id}/set")
def set_device(dev_id: str, cmd: Command):
    dev = devices.get(dev_id)
    if dev is None:
        raise HTTPException(status_code=404, detail="équipement inconnu")
    try:
        dev.set(cmd.name, cmd.value, cmd.transition)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except DeviceError as err:
        raise HTTPException(status_code=502, detail=str(err)) from err
    return dev.state()


PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Domotique</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; }
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
<div id="blocks"></div>
<script>
const box = document.getElementById("blocks");
const labels = { on: "allumé", off: "éteint", unreachable: "injoignable" };
const views = [];

async function send(dev, name, value) {
  await fetch("/api/devices/" + dev + "/set", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, value }),
  });
  refresh();
}

// Les contrôles ne sont pas écrasés par le rafraîchissement pendant qu'on les manipule.
function slider(dev, name, text, min, max) {
  const wrap = document.createElement("label");
  const input = Object.assign(document.createElement("input"), { type: "range", min, max });
  let busy = false;
  input.oninput = () => { busy = true; };
  input.onchange = async () => { await send(dev, name, +input.value); busy = false; };
  wrap.append(text, input);
  return { el: wrap, set: s => { if (!busy && s[name] !== undefined) input.value = s[name]; } };
}

function colorPicker(dev) {
  const input = Object.assign(document.createElement("input"), { type: "color" });
  let busy = false;
  input.oninput = () => { busy = true; };
  input.onchange = async () => { await send(dev, "rgb", input.value); busy = false; };
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
  return { el, update: s => {
    el.classList.toggle("down", s.power === "unreachable");
    parts.forEach(p => p.set(s));
  } };
}

async function refresh() {
  const data = await (await fetch("/api/blocks")).json();
  if (!views.length) {
    data.forEach(b => { const v = build(b); views.push(v); box.append(v.el); });
  }
  data.forEach((b, i) => views[i].update(b.state));
}

refresh();
setInterval(refresh, 3000);
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
