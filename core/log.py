"""Configuration des journaux."""

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


def _http_problem(record: logging.LogRecord) -> bool:
    """Requête HTTP journalisée par uvicorn : seulement les erreurs (statut 400 et plus)."""
    return not isinstance(record.args, tuple) or record.args[-1] >= 400


def _not_websocket_routine(record: logging.LogRecord) -> bool:
    """Ouverture et fermeture d'un WebSocket (une par page ouverte) : pas journalisées."""
    message = str(record.msg)
    return "[accepted]" not in message and message not in ("connection open", "connection closed")


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
    # uvicorn (lancé avec log_config=None) passe par ces mêmes journaux
    logging.getLogger("uvicorn.access").addFilter(_http_problem)
    logging.getLogger("uvicorn.error").addFilter(_not_websocket_routine)

    scripts = logging.getLogger("automations")
    scripts.setLevel(logging.DEBUG)
    scripts.propagate = False  # ne remonte ni dans la console ni dans system.log
    scripts.addHandler(_file_handler("automations.log"))
    logging.getLogger("habctrl").info("------------- Démarrage -------------")
