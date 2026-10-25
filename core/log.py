"""Configuration des journaux."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
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


def _uvicorn_server(record: logging.LogRecord) -> bool:
    """Journal général d'uvicorn (nommé « uvicorn.error » même pour ses INFO) : affiché
    « uvicorn » ; ouverture et fermeture d'un WebSocket (une par page ouverte) : pas journalisées."""
    record.name = "uvicorn"
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
    logging.getLogger("uvicorn.error").addFilter(_uvicorn_server)

    scripts = logging.getLogger("automations")
    scripts.setLevel(logging.DEBUG)
    scripts.propagate = False  # ne remonte ni dans la console ni dans system.log
    scripts.addHandler(_file_handler("automations.log"))
    logging.getLogger("habctrl").info("------------- Démarrage -------------")


class _Collector(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.exc_info and (err := record.exc_info[1]):
            message += f" ({type(err).__name__} : {err})"
        self.messages.append(message)


@contextmanager
def collect_errors() -> Iterator[list[str]]:
    """Messages des erreurs journalisées (niveau ERROR et plus) pendant le bloc, sans la pile
    d'appels : de quoi signaler une configuration incorrecte."""
    collector = _Collector()
    root = logging.getLogger()
    root.addHandler(collector)
    try:
        yield collector.messages
    finally:
        root.removeHandler(collector)
