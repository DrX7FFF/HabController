// Tracé des graphiques : seul fichier qui connaît la bibliothèque (uPlot, dans web/vendor/,
// chargée par index.html).
// Pour en changer, réécrire ce fichier en gardant createPlot().
//
// createPlot(container, series, group, onZoom) -> { setData(points, start, end), destroy() }
//   series : [{ label, unit, type: "number" | "bool" }]
//   points : pour chaque série, dans le même ordre, [[ms, valeur], ...] triés
//   start, end : période affichée (ms) ; la dernière valeur de chaque série tient jusqu'à end
//     (ou jusqu'à maintenant si end est dans le futur)
//   group : les graphiques d'un même groupe partagent le curseur et le zoom
//   onZoom(zoomed) : appelé à chaque changement de la plage affichée ; zoomed vaut true si
//     elle est plus étroite que la période (sélection à la souris), false au retour
//     (double-clic, ou setData)

const COLORS = ["#2a7ab0", "#d9822b", "#3f9b52", "#a14fb5", "#c4423f", "#6b7280"];
const HEIGHT = 260;
const BOOL_ROW = 1.4;     // hauteur d'une bande allumé / éteint (la bande pleine fait 1)
const BOOL_SHARE = 0.25;  // part de la hauteur réservée aux bandes, sous les courbes

const timeFmt = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit" });
const dayFmt = new Intl.DateTimeFormat("fr-FR", { weekday: "short", day: "numeric", month: "numeric" });
const fullFmt = new Intl.DateTimeFormat("fr-FR", { dateStyle: "medium", timeStyle: "short" });
const numFmt = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 });

const groups = new Map();  // groupe -> graphiques uPlot

// Graduation du temps : heures, et le jour à minuit ou quand l'écart dépasse un jour.
function timeTicks(u, splits, axisIdx, space, incr) {
  return splits.map(s => {
    const d = new Date(s * 1000);
    const midnight = d.getHours() === 0 && d.getMinutes() === 0;
    return incr >= 86400 || midnight ? dayFmt.format(d) : timeFmt.format(d);
  });
}

// Toutes les séries sur les mêmes instants : null là où une série n'a pas de point, sauf
// pour celles de hold (booléens) qui gardent leur valeur jusqu'au changement suivant.
function align(points, hold, end) {
  const times = [...new Set(points.flatMap(p => p.map(([t]) => t)).concat([end]))]
    .sort((a, b) => a - b);
  const index = new Map(times.map((t, i) => [t, i]));
  const columns = points.map((p, i) => {
    const col = new Array(times.length).fill(null);
    p.forEach(([t, v]) => { col[index.get(t)] = v; });
    if (p.length && p[p.length - 1][0] < end) col[times.length - 1] = p[p.length - 1][1];
    if (hold[i]) {
      const first = p.length ? index.get(p[0][0]) : col.length;
      for (let k = first + 1; k < col.length; k++) if (col[k] === null) col[k] = col[k - 1];
    }
    return col;
  });
  return [times.map(t => t / 1000), ...columns];
}

export function createPlot(container, series, group, onZoom) {
  const units = [...new Set(series.filter(s => s.type === "number").map(s => s.unit))];
  const bools = series.filter(s => s.type === "bool");
  const realMin = {};  // échelle -> plus petite valeur réelle (graduations en dessous masquées)

  const scales = { x: { time: true } };
  units.forEach(unit => {
    scales[unit] = { range: (u, min, max) => {
      const span = max - min || Math.abs(max) || 1;
      const below = bools.length ? span * BOOL_SHARE / (1 - BOOL_SHARE) : span * 0.05;
      return [min - below, max + span * 0.05];
    } };
  });
  if (bools.length) {
    const height = bools.length * BOOL_ROW;
    scales.bool = { range: [0, units.length ? height / BOOL_SHARE : height] };
  }

  const axes = [{ values: timeTicks }];
  units.forEach((unit, i) => axes.push({
    scale: unit,
    side: i % 2 ? 1 : 3,
    grid: { show: i === 0 },
    filter: (u, splits) => splits.map(v => (v >= realMin[unit] ? v : null)),
    values: (u, splits) => splits.map(v => (v == null ? "" : `${numFmt.format(v)} ${unit}`.trim())),
  }));

  let boolIndex = 0;
  const uSeries = [{ label: "Date", value: (u, v) => (v == null ? "—" : fullFmt.format(new Date(v * 1000))) }];
  series.forEach((s, i) => {
    const color = COLORS[i % COLORS.length];
    if (s.type === "bool") {
      const offset = boolIndex++ * BOOL_ROW;
      uSeries.push({
        label: s.label, scale: "bool", stroke: color, fill: color + "55", fillTo: offset,
        paths: uPlot.paths.stepped({ align: 1 }), spanGaps: true, points: { show: false },
        value: (u, v) => (v == null ? "—" : v - offset >= 0.5 ? "oui" : "non"),
      });
    } else {
      uSeries.push({
        label: s.label, scale: s.unit, stroke: color, width: 1.5, spanGaps: true,
        value: (u, v) => (v == null ? "—" : `${numFmt.format(v)} ${s.unit}`.trim()),
      });
    }
  });

  const members = groups.get(group) || new Set();
  groups.set(group, members);
  let syncing = false;
  let period = [0, 0];  // start, end du dernier setData (s)
  const plot = new uPlot({
    width: container.clientWidth, height: HEIGHT,
    scales, axes, series: uSeries,
    cursor: {
      sync: { key: group },
      // double-clic : retour à la période demandée (pas à l'étendue des données)
      bind: { dblclick: u => () => { u.setScale("x", { min: period[0], max: period[1] }); return null; } },
    },
    hooks: { setScale: [(u, key) => {
      if (key !== "x") return;
      if (onZoom) onZoom(u.scales.x.min > period[0] + 1 || u.scales.x.max < period[1] - 1);
      if (syncing) return;
      syncing = true;  // le zoom se propage aux autres graphiques du groupe
      members.forEach(other => {
        if (other !== u) other.setScale("x", { min: u.scales.x.min, max: u.scales.x.max });
      });
      syncing = false;
    }] },
  }, [[], ...series.map(() => [])], container);
  members.add(plot);

  const resize = new ResizeObserver(() => plot.setSize({ width: container.clientWidth, height: HEIGHT }));
  resize.observe(container);

  return {
    setData(points, start, end) {
      units.forEach(unit => {
        const values = points.filter((p, i) => series[i].type === "number" && series[i].unit === unit)
          .flatMap(p => p.map(([, v]) => v));
        realMin[unit] = values.length ? Math.min(...values) : 0;
      });
      let b = 0;
      const shifted = points.map((p, i) => {
        if (series[i].type !== "bool") return p;
        const offset = b++ * BOOL_ROW;
        return p.map(([t, v]) => [t, offset + (v ? 1 : 0)]);
      });
      // sans recalcul des échelles : celle de x est posée juste après (un seul onZoom)
      // la dernière valeur tient jusqu'à end, mais pas au-delà de maintenant
      const until = Math.min(end, Date.now());
      plot.setData(align(shifted, series.map(s => s.type === "bool"), until), false);
      period = [start / 1000, end / 1000];
      plot.setScale("x", { min: period[0], max: period[1] });
    },
    destroy() {
      resize.disconnect();
      members.delete(plot);
      plot.destroy();
    },
  };
}
