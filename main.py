from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from devices import load_devices

devices = load_devices()
app = FastAPI()


def describe(dev, state=None) -> dict:
    return {"id": dev.id, "name": dev.name, **(state or dev.state())}


@app.get("/api/devices")
def list_devices():
    with ThreadPoolExecutor() as pool:  # états lus en parallèle
        states = list(pool.map(lambda d: d.state(), devices.values()))
    return [describe(d, s) for d, s in zip(devices.values(), states)]


@app.post("/api/devices/{dev_id}/toggle")
def toggle(dev_id: str):
    dev = devices.get(dev_id)
    if dev is None:
        raise HTTPException(status_code=404, detail="équipement inconnu")
    try:
        dev.toggle()
    except Exception:
        pass
    return describe(dev)


PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Domotique</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; }
  #devices { display: flex; flex-wrap: wrap; gap: 1rem; }
  button { font-size: 1.3rem; padding: 1.2rem 2rem; border-radius: 1rem; border: 2px solid #888; cursor: pointer; }
  button.on { background: #ffd54a; border-color: #c9a100; }
  button.off { background: #ddd; }
  button.unreachable { background: #f5b5b5; border-color: #b00; }
</style>
</head>
<body>
<div id="devices"></div>
<script>
const box = document.getElementById("devices");
const labels = { on: "allumé", off: "éteint", unreachable: "injoignable" };

function render(devs) {
  box.replaceChildren(...devs.map(d => {
    const b = document.createElement("button");
    b.className = d.power;
    b.textContent = d.name + " : " + (labels[d.power] || d.power);
    b.onclick = async () => {
      await fetch("/api/devices/" + d.id + "/toggle", { method: "POST" });
      refresh();
    };
    return b;
  }));
}
async function refresh() { render(await (await fetch("/api/devices")).json()); }

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
