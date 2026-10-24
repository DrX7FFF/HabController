// Page /graphiques : les graphiques de dashboard/charts.toml, sur une période au choix.
// Glisser sur un graphique zoome (tous suivent) ; pendant le zoom, les réglages laissent la
// place à « Annuler zoom » (ou double-clic), et le Direct continue de recevoir sans redessiner.
// « Direct » : les valeurs enregistrées par le datalog arrivent par le WebSocket et s'ajoutent
// aux courbes ; la période glisse pour finir à la dernière valeur reçue.

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const PERIODS = [["5 min", 5 * MINUTE], ["20 min", 20 * MINUTE], ["1 h", HOUR], ["4 h", 4 * HOUR],
  ["12 h", 12 * HOUR], ["1 jour", DAY], ["4 jours", 4 * DAY], ["1 semaine", 7 * DAY],
  ["1 mois", 30 * DAY]];
const box = document.getElementById("charts");
const banner = document.getElementById("banner");
const note = document.getElementById("note");
const liveBox = document.getElementById("live");
const controls = document.getElementById("controls");
const unzoom = document.getElementById("unzoom");
let period = DAY;
let zoomed = false;
let pending = false;  // valeurs reçues en Direct pendant le zoom, pas encore dessinées
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
  const select = document.getElementById("period");
  select.append(...PERIODS.map(([label, ms]) => new Option(label, ms, false, ms === period)));
  select.onchange = () => { period = +select.value; refresh(); };
  document.getElementById("reload").onclick = refresh;
  unzoom.onclick = draw;  // redessine sur la période (à jour si Direct), ce qui sort du zoom
  liveBox.onchange = () => (liveBox.checked ? connect() : disconnect());
}

function setZoomed(value) {
  zoomed = value;
  if (!value && pending) redraw = redraw || requestAnimationFrame(draw);
  controls.hidden = value;
  unzoom.hidden = !value;
}

function draw() {
  redraw = null;
  pending = false;
  views.forEach(v => v.plot.setData(v.points, start, end));
}

async function refresh() {
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

// Oublie les points sortis de la période, sauf le dernier, ramené à start (état au début).
function trim(points) {
  let first = 0;
  while (first + 1 < points.length && points[first + 1][0] <= start) first++;
  const kept = points.slice(first);
  if (kept.length && kept[0][0] < start) kept[0] = [start, kept[0][1]];
  return kept;
}

// Les points arrivent tels que le datalog les enregistre (hystérésis comprise) : le direct
// montre la même chose que l'historique relu.
function receive(m) {
  if (m.type !== "datalog") return;
  let changed = false;
  let at = 0;
  m.points.forEach(([path, t, value]) => views.forEach(v => v.chart.series.forEach((s, i) => {
    if (s.path !== path) return;
    if (s.type === "bool" ? typeof value !== "boolean" : typeof value !== "number") return;
    const points = v.points[i];
    const last = points[points.length - 1];
    if (last && last[0] >= t) return;  // déjà connu
    points.push([t, value]);
    changed = true;
    at = Math.max(at, t);
  })));
  if (!changed) return;
  if (at > end) {  // la période glisse jusqu'à la nouvelle valeur, même durée
    end = at;
    start = end - period;
    views.forEach(v => { v.points = v.points.map(trim); });
  }
  if (zoomed) pending = true;
  else redraw = redraw || requestAnimationFrame(draw);
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
    return { chart, plot: createPlot(area, chart.series, "historique", setZoomed), points: chart.series.map(() => []) };
  });
  refresh();
}

main();
