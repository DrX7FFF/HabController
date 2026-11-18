"""Correctifs zha-quirks : seuls ceux des appareils de la base sont chargés.

L'index (fabricant, modèle) → modules est construit par ce fichier lancé à part
(`python quirks.py <index>`), qui charge tous les correctifs puis rend sa mémoire en se terminant."""

import importlib
import json
import sqlite3
import sys
from importlib.metadata import version


def build(index: str) -> None:
    """Charge tous les correctifs et écrit l'index des modules par (fabricant, modèle)."""
    import zhaquirks
    from zha.quirks import DEVICE_REGISTRY

    zhaquirks.setup()
    models = [
        [manufacturer, model, entry.source.module]
        for (manufacturer, model), entries in DEVICE_REGISTRY._registry.items()
        for entry in entries
    ]
    every = [entry.source.module for entry in DEVICE_REGISTRY._wildcard_registry]
    with open(index, "w", encoding="utf-8") as file:
        json.dump({"version": version("zha-quirks"), "all": every, "models": models}, file)


def read_index(index) -> dict | None:
    """Index à jour pour la version installée de zha-quirks, sinon None."""
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("version") == version("zha-quirks") else None


def paired_models(db) -> set[tuple[str, str]]:
    """(fabricant, modèle) de chaque endpoint des appareils de la base zigpy."""
    if not db.exists():
        return set()
    from zigpy.appdb import DB_VERSION

    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            f"SELECT ieee, endpoint_id, attr_id, value FROM attributes_cache_v{DB_VERSION}"
            " WHERE cluster_id = 0 AND attr_id IN (4, 5)"
        ).fetchall()
    finally:
        con.close()
    found: dict = {}
    for ieee, endpoint, attr, value in rows:
        found.setdefault((ieee, endpoint), {})[attr] = str(value)
    return {(attrs.get(4), attrs.get(5)) for attrs in found.values()}


def load(index: dict, db) -> list[str]:
    """Importe les modules utiles aux appareils de la base ; renvoie leurs noms."""
    import zhaquirks

    models = paired_models(db)
    modules = set(index["all"]) | {
        module
        for manufacturer, model, module in index["models"]
        if any(
            manufacturer in (None, paired[0]) and model in (None, paired[1])
            for paired in models
        )
    }
    for module in sorted(modules):
        importlib.import_module(module)
    zhaquirks._register_pending_quirks()  # correctifs v1 : passés au registre de ZHA
    return sorted(modules)


if __name__ == "__main__":
    build(sys.argv[1])
