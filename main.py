import threading

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from yeelight import Bulb

BULB_IP = "192.168.1.110"

bulb = Bulb(BULB_IP, auto_on=False)  # port 55443 par défaut
lock = threading.Lock()  # une seule commande à la fois sur la connexion de l'ampoule
app = FastAPI()


def read_state() -> dict:
    try:
        with lock:
            return {"power": bulb.get_properties()["power"]}
    except Exception:
        return {"power": "unreachable"}


@app.get("/api/state")
def get_state():
    return read_state()


@app.post("/api/toggle")
def toggle():
    try:
        with lock:
            bulb.toggle()
    except Exception:
        pass
    return read_state()


PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Salon</title>
<style>
  body { font-family: system-ui, sans-serif; display: grid; place-items: center; height: 100vh; margin: 0; }
  button { font-size: 1.5rem; padding: 1.5rem 3rem; border-radius: 1rem; border: 2px solid #888; cursor: pointer; }
  button.on { background: #ffd54a; border-color: #c9a100; }
  button.off { background: #ddd; }
  button.unreachable { background: #f5b5b5; border-color: #b00; }
</style>
</head>
<body>
<button id="btn" class="off">…</button>
<script>
const btn = document.getElementById("btn");
const labels = { on: "Salon : allumé", off: "Salon : éteint", unreachable: "Ampoule injoignable" };

function render(s) {
  btn.className = s.power;
  btn.textContent = labels[s.power] || s.power;
}
async function refresh() { render(await (await fetch("/api/state")).json()); }

btn.onclick = async () => render(await (await fetch("/api/toggle", { method: "POST" })).json());

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
