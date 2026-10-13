"""Journaux : un pour la vie du système, un pour les scripts d'automatisation.

- Système (pilotes, connexions, serveur, moteur d'automatisation) : console + temp/system.log
- Scripts (automations/*.py) : temp/automations.log seulement, jamais dans la console

Un script se journalise avec `log = logging.getLogger(__name__)` : son nom (automations.<script>)
suffit à l'envoyer dans le bon fichier.
"""

import logging
from logging.handlers import RotatingFileHandler

from core import TEMP_DIR

FORMAT = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s : %(message)s")


def _file_handler(name: str) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        TEMP_DIR / name, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(FORMAT)
    return handler


def setup() -> None:
    """À appeler une fois, avant le chargement des équipements et des automatisations."""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    console = logging.StreamHandler()
    console.setFormatter(FORMAT)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(_file_handler("system.log"))
    for library in ("zigpy", "bellows"):  # très bavardes en INFO : seulement les problèmes
        logging.getLogger(library).setLevel(logging.WARNING)

    scripts = logging.getLogger("automations")
    scripts.setLevel(logging.DEBUG)
    scripts.propagate = False  # ne remonte ni dans la console ni dans system.log
    scripts.addHandler(_file_handler("automations.log"))
