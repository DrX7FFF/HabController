// Onglet Commandes : un bloc par équipement de dashboard/screens.toml, tenu à jour par /ws.

import { banner, el, send, subscribe } from "/common.js";

const labels = { on: "allumé", off: "éteint", unreachable: "injoignable" };
// connected false, ou on pas encore lu : affiché injoignable
const onKey = s =>
  s.connected === false || s.on === undefined ? "unreachable" : s.on ? "on" : "off";
// Mesures affichées par les widgets sensor et plug, si l'équipement les suit (clé d'état,
// libellé, mise en forme)
const SENSOR = [
  ["temperature", "Température", v => `${v.toFixed(1)} °C`],
  ["humidity", "Humidité", v => `${Math.round(v)} %`],
  ["pressure", "Pression", v => `${Math.round(v)} hPa`],
  ["illuminance", "Luminosité", v => `${v} lx`],
  ["presence", "Présence", v => (v ? "oui" : "non")],
  ["open", "Ouverture", v => (v ? "ouvert" : "fermé")],
  ["battery", "Pile", v => `${v} %`],
];
const PLUG = [
  ["active_power", "Puissance", v => `${v} W`],
  ["voltage", "Tension", v => `${v} V`],
  ["current", "Intensité", v => `${v} A`],
  ["energy", "Énergie", v => `${v} kWh`],
];

const call = (device, method, ...args) => send({ type: "call", device, method, args });

// Les contrôles ne sont pas écrasés par un événement pendant qu'on les manipule.
function slider(dev, name, method, text, min, max) {
  const input = el("input", { type: "range", min, max });
  let busy = false;
  input.oninput = () => { busy = true; };
  input.onchange = () => { call(dev, method, +input.value); busy = false; };
  return { el: el("label", {}, text, input),
    set: s => { if (!busy && s[name] !== undefined) input.value = s[name]; } };
}

function colorPicker(dev) {
  const input = el("input", { type: "color" });
  let busy = false;
  input.oninput = () => { busy = true; };
  input.onchange = () => { call(dev, "set_rgb", input.value); busy = false; };
  return { el: input, set: s => { if (!busy && s.rgb) input.value = s.rgb; } };
}

// Mesures suivies (tracked) en lecture seule ; vide tant qu'aucune valeur n'est reçue.
function values(rows, measures) {
  const list = el("dl", { className: "measures" });
  const cells = rows.filter(([key]) => measures[key] === "tracked").map(([key, label, format]) => {
    const value = el("dd");
    list.append(el("dt", { textContent: label }), value);
    return s => { value.textContent = s[key] == null ? "" : format(s[key]); };
  });
  return { el: list, set: s => cells.forEach(set => set(s)) };
}

function power(dev) {
  const button = el("button");
  let on = false;
  button.onclick = () => call(dev, "switch", !on);
  return { el: button, set: s => {
    on = s.on === true;
    button.className = onKey(s);
    button.textContent = labels[onKey(s)];
  } };
}

// Afficheur RGB : nom de l'animation, précédente / pause-lecture / suivante.
function matrix(dev) {
  const name = el("div", { className: "pattern" });
  const prev = el("button", { textContent: "⏮", title: "Précédente" });
  const play = el("button", { title: "Pause / lecture" });
  const next = el("button", { textContent: "⏭", title: "Suivante" });
  let auto = true;
  prev.onclick = () => call(dev, "previous_pattern");
  next.onclick = () => call(dev, "next_pattern");
  play.onclick = () => call(dev, "set_auto", !auto);
  return { el: el("div", { className: "player" }, name, el("div", {}, prev, play, next)),
    set: s => {
      auto = s.auto !== false;
      name.textContent = s.pattern ?? "";
      play.textContent = auto ? "⏸" : "▶";
    } };
}

// Aspirateur : dernier nettoyage (lu au chargement et par Actualiser) et commandes, empilés.
function vacuum(dev) {
  const last = el("div");
  const buttons = [["clean", "Nettoyer"], ["pause", "Pause"], ["home", "Base"], ["refresh", "Actualiser"]]
    .map(([cmd, text]) => ({ el: el("button", { textContent: text,
      onclick: () => call(dev, cmd) }), set: () => {} }));
  return [{ el: last, set: s => {
    const c = s.last_clean;
    last.textContent = c ? `Dernier : ${new Date(c.start).toLocaleString("fr-FR",
      { dateStyle: "short", timeStyle: "short" })}, ${c.duration} min, ${c.area} m²` +
      (c.complete ? "" : ", interrompu") : "";
  } }, ...buttons];
}

// FP2 : une pastille par zone, couleur active si occupée, grise sinon.
function zones() {
  const box = el("div", { className: "zones" });
  return { el: box, set: s => box.replaceChildren(...Object.keys(s)
    .filter(k => k.startsWith("zone."))
    .map(k => el("span", { className: s[k] ? "on" : "", textContent: k.slice(5) }))) };
}

// Widget de chaque type d'équipement : ses parties, d'après l'état de surveillance des mesures.
const WIDGETS = {
  sensor: (dev, m) => [values(SENSOR, m)],
  fp2: (dev, m) => [zones(), values(SENSOR, m)],
  plug: (dev, m) => [power(dev), values(PLUG, m)],
  onoff: dev => [power(dev)],
  matrix: dev => [power(dev), matrix(dev)],
  vacuum: dev => vacuum(dev),
  light: (dev, m) => [
    power(dev),
    m.brightness === "tracked" && slider(dev, "brightness", "set_brightness", "Intensité", 1, 100),
    m.color_temp === "tracked" && slider(dev, "color_temp", "set_color_temp", "Température", 1700, 6500),
    m.rgb === "tracked" && colorPicker(dev),
  ].filter(Boolean),
};

// Cadre et titre seuls tant que l'équipement n'est pas prêt (measures null).
function build(b) {
  const block = el("div", { className: "block card" });
  const title = el("h2", { textContent: b.title });
  let parts = [];
  let state = {};
  const update = s => {
    state = s;
    block.classList.toggle("down", s.connected === false);
    parts.forEach(p => p.set(s));
  };
  const measures = m => {
    parts = m ? WIDGETS[b.widget](b.device, m) : [];
    block.replaceChildren(title, ...parts.map(p => p.el));
    update(state);
  };
  measures(b.measures);
  return { el: block, device: b.device, update, measures };
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
    } else if (m.type === "measures") {
      views.filter(v => v.device === m.device).forEach(v => v.measures(m.measures));
    } else if (m.type === "error") {
      show(m.detail);
      clearTimeout(hide);
      hide = setTimeout(() => show(""), 4000);
    } else if (m.type === "closed") {
      views.forEach(v => v.update({ connected: false }));
    }
  });
}
