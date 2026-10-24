const box = document.getElementById("blocks");
const banner = document.getElementById("banner");
const labels = { on: "allumé", off: "éteint", unreachable: "injoignable" };
// connected false, ou on pas encore lu : affiché injoignable
const onKey = s =>
  s.connected === false || s.on === undefined ? "unreachable" : s.on ? "on" : "off";
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
  power.onclick = () => send(b.device, "on", "toggle");
  const parts = [{ el: power, set: s => {
    power.className = onKey(s);
    power.textContent = labels[onKey(s)];
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
    el.classList.toggle("down", s.connected === false);
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
    views.forEach(v => v.update({ connected: false }));
    show("Connexion au serveur perdue…");
    setTimeout(connect, 2000);
  };
}

connect();
