"""Lecture des graphiques de Config/dashboard/charts.toml."""

import logging
import tomllib
from pathlib import Path

from core import CONFIG_DIR

log = logging.getLogger(__name__)

CHARTS = CONFIG_DIR / "dashboard" / "charts.toml"
TYPES = {"number", "bool"}
MAX_POINTS = 1000  # par série envoyée à la page


def downsample(points: list[list], max_points: int = MAX_POINTS) -> list[list]:
    """Réduit une série numérique à environ max_points : par tranche de temps, le minimum et
    le maximum (dans leur ordre), pour garder les pics, et la dernière valeur. Inchangée si
    déjà assez courte."""
    if len(points) <= max_points:
        return points
    buckets = (max_points - 1) // 2
    start, span = points[0][0], points[-1][0] - points[0][0] + 1
    groups: list[list[list]] = [[] for _ in range(buckets)]
    for point in points:
        groups[(point[0] - start) * buckets // span].append(point)
    reduced = []
    for group in groups:
        if group:
            low = min(group, key=lambda p: p[1])
            high = max(group, key=lambda p: p[1])
            reduced.extend(sorted({id(low): low, id(high): high}.values()))
    if reduced[-1] is not points[-1]:  # la dernière valeur, celle qui tient jusqu'à maintenant
        reduced.append(points[-1])
    return reduced


def load_charts(path: Path = CHARTS) -> list[dict]:
    """Graphiques ; graphique incorrect : ignoré, erreur au journal. Fichier absent : aucun
    graphique ; illisible : aucun graphique, erreur au journal."""
    try:
        conf = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        log.error("%s illisible, aucun graphique : %s", path.name, err)
        return []
    charts = []
    for chart in conf.get("charts", []):
        try:
            charts.append(read_chart(chart))
        except (ValueError, TypeError, AttributeError) as err:
            log.error("graphique ignoré : %s", err)
    return charts


def read_chart(chart: dict) -> dict:
    """Un graphique de charts.toml ; lève ValueError s'il est incorrect."""
    title = str(chart.get("title", ""))
    series = []
    for s in chart.get("series", []):
        if "path" not in s:
            raise ValueError(f"« {title} » : série sans path")
        path = str(s["path"]).strip("/")
        kind = s.get("type", "number")
        if kind not in TYPES:
            raise ValueError(f"« {title} » : type inconnu « {kind} »")
        series.append(
            {
                "path": path,
                "label": s.get("label", path.rsplit("/", 1)[-1]),
                "unit": s.get("unit", ""),
                "type": kind,
            }
        )
    if not series:
        raise ValueError(f"« {title} » : aucune série")
    return {"title": title, "series": series}
