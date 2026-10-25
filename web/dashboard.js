const box = document.getElementById("blocks");
const banner = document.getElementById("banner");
const labels = { on: "allumé", off: "éteint", unreachable: "injoignable" };
// connected false, ou on pas encore lu : affiché injoignable
const onKey = s =>
  s.connected === false || s.on === undefined ? "unreachable" : s.on ? "on" : "off";
// Widget sensor : mesures affichées, si l'équipement les a (clé d'état, libellé, mise en forme)
const MEASURES = [
  ["temperature", "Température", v => `${v.toFixed(1)} °C`],
  ["humidity", "Humidité", v => `${Math.round(v)} %`],
  ["pressure", "Pression", v => `${Math.round(v)} hPa`],
  ["illuminance", "Luminosité", v => `${v} lx`],
  ["presence", "Présence", v => (v ? "oui" : "non")],
  ["open", "Ouverture", v => (v ? "ouvert" : "fermé")],
  ["battery", "Pile", v => `${v} %`],
];
let views = [];
let ws;
let notice = "";  // erreurs de configuration : bandeau affiché à défaut d'autre message

function send(device, name, value) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "set", device, name, value }));
  }
}

function show(text) {
  banner.textContent = text || notice;
  banner.hidden = !banner.textContent;
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

// Mesures en lecture seule ; sans mesure, l'état (un capteur sur pile attend son premier message).
function measures(s) {
  const list = document.createElement("dl");
  list.className = "measures";
  const shown = MEASURES.filter(([key]) => s[key] !== undefined && s[key] !== null);
  if (!shown.length) {
    const empty = Object.assign(document.createElement("dd"),
      { textContent: s.connected === false ? "injoignable" : "en attente de mesure" });
    list.append(empty);
  }
  shown.forEach(([key, label, format]) => {
    list.append(Object.assign(document.createElement("dt"), { textContent: label }),
      Object.assign(document.createElement("dd"), { textContent: format(s[key]) }));
  });
  return list;
}

function build(b) {
  const el = document.createElement("div");
  el.className = "block";
  const title = document.createElement("h2");
  title.textContent = b.title;
  if (b.widget === "sensor") {
    let list = measures({});
    el.append(title, list);
    return { el, device: b.device, update: s => {
      el.classList.toggle("down", s.connected === false);
      const next = measures(s);
      list.replaceWith(next);
      list = next;
    } };
  }
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
    notice = m.errors.length
      ? `Configuration : ${m.errors.length} erreur(s), voir Administration.` : "";
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
