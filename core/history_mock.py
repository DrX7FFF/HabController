"""Historique simulé quand le datalog n'est pas configuré (tests, essais sans Firebase) :
mêmes réponses que Datalog.read().

Les valeurs ne dépendent que du chemin et de l'heure : deux requêtes sur des périodes qui se
recouvrent donnent les mêmes points. Comme le datalog, un booléen n'a un point qu'à chaque
changement ; le premier point donne l'état au début de la période.
"""

import math
import random
import zlib

STEP = 5 * 60_000  # ms entre deux mesures
SLOT = 30 * 60_000  # ms entre deux changements possibles d'un booléen
DAY = 86_400_000

# attribut -> (valeur de base, amplitude du cycle jour/nuit, bruit)
PROFILES = {
    "active_power": (25, 20, 8),
    "illuminance": (150, 150, 20),
    "temperature": (20.5, 1.5, 0.2),
    "humidity": (55, 8, 1.5),
    "pressure": (1013, 3, 0.5),
    "battery": (80, 0, 0.3),
}
DEFAULT_PROFILE = (50, 20, 5)


def mock_history(path: str, kind: str, start: int, end: int) -> list[list]:
    seed = zlib.crc32(path.encode())
    if kind == "bool":
        return _bool_points(seed, start, end)
    attr = path.rsplit("/", 1)[-1]
    return _number_points(seed, PROFILES.get(attr, DEFAULT_PROFILE), start, end)


def _number_points(seed: int, profile: tuple, start: int, end: int) -> list[list]:
    base, amplitude, noise = profile
    points = []
    for t in range(start - start % STEP, end + 1, STEP):
        cycle = math.sin(2 * math.pi * ((t % DAY) / DAY - 0.375))  # maximum vers 15 h
        jitter = random.Random(seed ^ t).gauss(0, noise)
        points.append([t, round(max(0, base + amplitude * cycle + jitter), 1)])
    return points


def _bool_points(seed: int, start: int, end: int) -> list[list]:
    points, last = [], None
    for t in range(start - start % SLOT, end + 1, SLOT):
        value = random.Random(seed ^ t).random() < 0.3
        if value != last:
            points.append([max(t, start), value])
            last = value
    return points
