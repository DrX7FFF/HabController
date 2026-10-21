// Page /graphiques : les graphiques de dashboard/charts.toml, sur une période au choix.
// Glisser sur un graphique zoome (tous suivent), double-clic : retour à la période.

const DAY = 86_400_000;
const PERIODS = [["24 h", 1], ["7 jours", 7], ["30 jours", 30]];
const box = document.getElementById("charts");
const banner = document.getElementById("banner");
const note = document.getElementById("note");
let days = 1;
let views = [];  // { chart, plot }

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
  PERIODS.forEach(([label, n]) => {
    const button = Object.assign(document.createElement("button"), { textContent: label });
    button.onclick = () => { days = n; refresh(); };
    button.dataset.days = n;
    bar.append(button);
  });
  const reload = Object.assign(document.createElement("button"), { textContent: "Actualiser" });
  reload.onclick = refresh;
  bar.append(reload);
}

async function refresh() {
  document.querySelectorAll("#periods button[data-days]").forEach(b => {
    b.classList.toggle("selected", +b.dataset.days === days);
  });
  const end = Date.now();
  const start = end - days * DAY;
  try {
    const results = await Promise.all(views.map(({ chart }) => Promise.all(chart.series.map(s =>
      getJson(`/api/history?path=${encodeURIComponent(s.path)}&start=${start}&end=${end}`)))));
    results.forEach((answers, i) => views[i].plot.setData(answers.map(a => a.points), start, end));
    note.hidden = !results.flat().some(a => a.mock);
    show("");
  } catch (err) {
    show(`Lecture de l'historique impossible (${err.message})`);
  }
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
    return { chart, plot: createPlot(area, chart.series, "historique") };
  });
  refresh();
}

main();
