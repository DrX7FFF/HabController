"""Graphiques : dashboard/charts.toml décrit les graphiques de la page /graphiques.

    [[charts]]
    title = "Prise salon"
    series = [
      { path = "devices/prise_salon/active_power", label = "Puissance", unit = "W" },
      { path = "devices/prise_salon/on", label = "Allumée", type = "bool" },
    ]

`path` est un chemin du datalog (voir core/datalog.py). `type` : number (par défaut, courbe)
ou bool (bande allumé / éteint sous la courbe). Les séries de même `unit` partagent un axe.
Fichier absent : aucun graphique.
"""

import tomllib
from pathlib import Path

from core import CONFIG_DIR

CHARTS = CONFIG_DIR / "dashboard" / "charts.toml"
TYPES = {"number", "bool"}


def load_charts(path: Path = CHARTS) -> list[dict]:
    if not path.exists():
        return []
    conf = tomllib.loads(path.read_text(encoding="utf-8"))
    charts = []
    for chart in conf.get("charts", []):
        title = chart.get("title", "")
        series = []
        for s in chart.get("series", []):
            if "path" not in s:
                raise ValueError(f"graphique « {title} » : série sans path")
            path = s["path"].strip("/")
            kind = s.get("type", "number")
            if kind not in TYPES:
                raise ValueError(f"graphique « {title} » : type inconnu « {kind} »")
            series.append(
                {
                    "path": path,
                    "label": s.get("label", path.rsplit("/", 1)[-1]),
                    "unit": s.get("unit", ""),
                    "type": kind,
                }
            )
        if not series:
            raise ValueError(f"graphique « {title} » : aucune série")
        charts.append({"title": title, "series": series})
    return charts
