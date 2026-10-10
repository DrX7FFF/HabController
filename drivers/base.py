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

    def state(self) -> dict:
        raise NotImplementedError

    def set(self, name: str, value: str | int, transition: int | None = None) -> None:
        raise NotImplementedError
