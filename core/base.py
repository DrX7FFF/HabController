from collections.abc import Callable


class DeviceError(Exception):
    """Équipement injoignable ou commande refusée."""


class Device:
    """Base commune des pilotes : identité, capacités, état et commandes."""

    capabilities: frozenset[str] = frozenset()

    def __init__(self, id: str, name: str, capabilities: list[str] | None = None):
        self.id = id
        self.name = name
        if capabilities is not None:  # surcharge possible depuis devices.toml
            self.capabilities = frozenset(capabilities)
        self._listeners: list[Callable[[dict], None]] = []
        self._last_notified: dict | None = None

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        """callback(state) est appelé à chaque changement d'état de l'équipement."""
        self._listeners.append(callback)

    def _notify(self) -> None:
        """À appeler après toute mise à jour de l'état ; ne diffuse que les vrais changements."""
        state = self.state()
        if state != self._last_notified:
            self._last_notified = state
            for callback in self._listeners:
                callback(state)

    async def start(self) -> None:
        """Appelé au démarrage de l'application (connexion, écoute...)."""

    async def stop(self) -> None:
        """Appelé à l'arrêt de l'application."""

    def state(self) -> dict:
        """État courant, lu en mémoire : ne doit jamais interroger l'équipement."""
        raise NotImplementedError

    async def set(self, name: str, value: str | int, transition: int | None = None) -> None:
        raise NotImplementedError
