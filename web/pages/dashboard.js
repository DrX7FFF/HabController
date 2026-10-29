// Onglet Commandes : un bloc par équipement de dashboard/screens.toml, tenu à jour par /ws.

import { banner, el, send, subscribe } from "/common.js";

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

const command = (device, name, value) => send({ type: "set", device, name, value });

// Les contrôles ne sont pas écrasés par un événement pendant qu'on les manipule.
function slider(dev, name, text, min, max) {
  const input = el("input", { type: "range", min, max });
  let busy = false;
  input.oninput = () => { busy = true; };
  input.onchange = () => { command(dev, name, +input.value); busy = false; };
  return { el: el("label", {}, text, input),
    set: s => { if (!busy && s[name] !== undefined) input.value = s[name]; } };
}

function colorPicker(dev) {
  const input = el("input", { type: "color" });
  let busy = false;
  input.oninput = () => { busy = true; };
  input.onchange = () => { command(dev, "rgb", input.value); busy = false; };
  return { el: input, set: s => { if (!busy && s.rgb) input.value = s.rgb; } };
}

// Mesures en lecture seule ; sans mesure, l'état (un capteur sur pile attend son premier message).
function measures(s) {
  const list = el("dl", { className: "measures" });
  const shown = MEASURES.filter(([key]) => s[key] !== undefined && s[key] !== null);
  if (!shown.length) {
    list.append(el("dd",
      { textContent: s.connected === false ? "injoignable" : "en attente de mesure" }));
  }
  shown.forEach(([key, label, format]) => {
    list.append(el("dt", { textContent: label }), el("dd", { textContent: format(s[key]) }));
  });
  return list;
}

function build(b) {
  const block = el("div", { className: "block card" });
  const title = el("h2", { textContent: b.title });
  if (b.widget === "sensor") {
    let list = measures({});
    block.append(title, list);
    return { el: block, device: b.device, update: s => {
      block.classList.toggle("down", s.connected === false);
      const next = measures(s);
      list.replaceWith(next);
      list = next;
    } };
  }
  const power = el("button");
  power.onclick = () => command(b.device, "on", "toggle");
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
  block.append(title, ...parts.map(p => p.el));
  return { el: block, device: b.device, update: s => {
    block.classList.toggle("down", s.connected === false);
    parts.forEach(p => p.set(s));
  } };
}

export function mount(page) {
  const box = page.querySelector("#blocks");
  const say = banner(page);
  const show = text => say(text || notice);  // à défaut de message : erreurs de configuration
  let notice = "";
  let views = [];
  let hide = null;  // effacement prévu du dernier message d'erreur

  subscribe(m => {
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
      clearTimeout(hide);
      hide = setTimeout(() => show(""), 4000);
    } else if (m.type === "closed") {
      views.forEach(v => v.update({ connected: false }));
    }
  });
}
