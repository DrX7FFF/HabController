"""Correctifs zha-quirks : seuls ceux des appareils présents sont chargés.

L'index (fabricant, modèle) → modules est construit par ce fichier lancé à part
(`python quirks.py <index>`), qui charge tous les correctifs puis rend sa mémoire en se terminant."""

import importlib
import json
import logging
import sys
from importlib.metadata import version

log = logging.getLogger(__name__)


def build(index: str) -> None:
    """Charge tous les correctifs et écrit l'index des modules par (fabricant, modèle)."""
    import zhaquirks
    from zha.quirks import DEVICE_REGISTRY

    zhaquirks.setup()
    models = [
        [manufacturer, model, entry.source.module]
        for (manufacturer, model), entries in DEVICE_REGISTRY._registry.items()
        for entry in entries
        if entry.source
    ]
    every = [entry.source.module for entry in DEVICE_REGISTRY._wildcard_registry if entry.source]
    with open(index, "w", encoding="utf-8") as file:
        json.dump({"version": version("zha-quirks"), "all": every, "models": models}, file)


def read_index(index) -> dict | None:
    """Index à jour pour la version installée de zha-quirks, sinon None."""
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("version") == version("zha-quirks") else None


def resolver(index: dict):
    """Charge les correctifs valables pour tous les appareils ; renvoie la fonction que zigpy
    appelle sur chaque appareil (chargé de la base ou appairé), qui charge d'abord les
    correctifs de son fabricant et de son modèle."""
    import zhaquirks
    from zha.quirks import DEVICE_REGISTRY

    by_model: dict = {}
    for manufacturer, model, module in index["models"]:
        by_model.setdefault((manufacturer, model), set()).add(module)

    def load(modules) -> None:
        for module in sorted(modules):
            if module not in sys.modules:
                importlib.import_module(module)
                log.info("Correctif zha-quirks chargé : %s", module)
        zhaquirks._register_pending_quirks()  # correctifs v1 : passés au registre de ZHA

    def resolve(device):
        manufacturer, model = device.manufacturer, device.model
        load(
            by_model.get((manufacturer, model), set())
            | by_model.get((manufacturer, None), set())
            | by_model.get((None, model), set())
        )
        return DEVICE_REGISTRY.resolve(device)

    load(index["all"])
    return resolve


if __name__ == "__main__":
    build(sys.argv[1])
