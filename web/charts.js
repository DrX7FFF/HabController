// Page /graphiques : les graphiques de dashboard/charts.toml, sur une période au choix.
// Glisser sur un graphique zoome (tous suivent), double-clic : retour à la période.
// « Direct » : les valeurs reçues par le WebSocket s'ajoutent aux courbes, et la période
// glisse pour finir à la dernière valeur reçue.

const MINUTE = 60_000;
const PERIODS = [["30 min", 30 * MINUTE], ["1 h", 60 * MINUTE], ["24 h", 1440 * MINUTE],
  ["7 jours", 7 * 1440 * MINUTE], ["30 jours", 30 * 1440 * MINUTE]];
const box = document.getElementById("charts");
const banner = document.getElementById("banner");
const note = document.getElementById("note");
const liveBox = document.getElementById("live");
let period = 1440 * MINUTE;
let start = 0;
let end = 0;
let views = [];  // { chart, plot, points: [[ms, valeur], ...] par série }
let ws = null;
let redraw = null;  // tracé en attente (au plus un par image)

function show(text) {
  banner.textContent = text;
  banner.hidden = !text;
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} : ${response.status}`);
  return response.json();
}

function toolbar() {
  const bar = document.getElementById("periods");
  PERIODS.forEach(([label, ms]) => {
    const button = Object.assign(document.createElement("button"), { textContent: label });
    button.onclick = () => { period = ms; refresh(); };
    button.dataset.period = ms;
    bar.append(button);
  });
  const reload = Object.assign(document.createElement("button"), { textContent: "Actualiser" });
  reload.onclick = refresh;
  bar.append(reload, liveBox.parentElement);  // « Direct » en dernier
  liveBox.onchange = () => (liveBox.checked ? connect() : disconnect());
}

function draw() {
  redraw = null;
  views.forEach(v => v.plot.setData(v.points, start, end));
}

async function refresh() {
  document.querySelectorAll("#periods button[data-period]").forEach(b => {
    b.classList.toggle("selected", +b.dataset.period === period);
  });
  end = Date.now();
  start = end - period;
  try {
    const results = await Promise.all(views.map(({ chart }) => Promise.all(chart.series.map(s =>
      getJson(`/api/history?path=${encodeURIComponent(s.path)}&start=${start}&end=${end}`)))));
    results.forEach((answers, i) => { views[i].points = answers.map(a => a.points); });
    draw();
    note.hidden = !results.flat().some(a => a.mock);
    show("");
  } catch (err) {
    show(`Lecture de l'historique impossible (${err.message})`);
  }
}

// --- Direct ------------------------------------------------------------------

// Chemin du datalog d'un attribut d'équipement (comme device_path() de core/datalog.py).
const devicePath = (device, attr) => `devices/${device}/${attr.replaceAll(".", "/")}`;

// Oublie les points sortis de la période, sauf le dernier, ramené à start (état au début).
function trim(points) {
  let first = 0;
  while (first + 1 < points.length && points[first + 1][0] <= start) first++;
  const kept = points.slice(first);
  if (kept.length && kept[0][0] < start) kept[0] = [start, kept[0][1]];
  return kept;
}

function receive(m) {
  if (m.type !== "state") return;
  const at = m.at || Date.now();
  let changed = false;
  views.forEach(v => v.chart.series.forEach((s, i) => {
    const attr = Object.keys(m.state).find(a => devicePath(m.device, a) === s.path);
    if (attr === undefined) return;
    const value = m.state[attr];
    if (s.type === "bool" ? typeof value !== "boolean" : typeof value !== "number") return;
    const points = v.points[i];
    const last = points[points.length - 1];
    if (last && (last[0] >= at || last[1] === value)) return;  // déjà connu ou inchangé
    points.push([at, value]);
    changed = true;
  }));
  if (!changed) return;
  if (at > end) {  // la période glisse jusqu'à la nouvelle valeur, même durée
    end = at;
    start = end - period;
    views.forEach(v => { v.points = v.points.map(trim); });
  }
  redraw = redraw || requestAnimationFrame(draw);
}

function connect() {
  const socket = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws = socket;
  socket.onmessage = e => receive(JSON.parse(e.data));
  socket.onopen = refresh;  // repart d'un historique à jour : rien de manqué entre-temps
  socket.onclose = () => {
    if (ws !== socket) return;  // fermé volontairement
    show("Direct : connexion au serveur perdue…");
    setTimeout(() => { if (ws === socket) connect(); }, 2000);
  };
}

function disconnect() {
  const socket = ws;
  ws = null;
  if (socket) socket.close();
}

async function main() {
  toolbar();
  let charts;
  try {
    charts = await getJson("/api/charts");
  } catch (err) {
    show(`Liste des graphiques illisible (${err.message})`);
    return;
  }
  if (!charts.length) {
    show("Aucun graphique : les décrire dans dashboard/charts.toml.");
    return;
  }
  views = charts.map(chart => {
    const card = document.createElement("section");
    card.className = "chart";
    const title = document.createElement("h2");
    title.textContent = chart.title;
    const area = document.createElement("div");
    card.append(title, area);
    box.append(card);
    return { chart, plot: createPlot(area, chart.series, "historique"), points: chart.series.map(() => []) };
  });
  refresh();
}

main();
