"""Pilote Roborock S5 Max : protocole miio local (UDP 54321), commandes et historique à la demande."""

import asyncio

from miio import DeviceException, RoborockVacuum

from core.base import Device, DeviceError

COMMANDS = ("clean", "pause", "home", "refresh")  # commandes acceptées par set("command", …)


class Roborock(Device):
    """Robot aspirateur : aucun état mémorisé, chaque appel interroge le robot."""

    WIDGET = "vacuum"

    def __init__(self, host: str, token: str):
        super().__init__()
        self._host = host
        self._vacuum = RoborockVacuum(host, token, model="roborock.vacuum.s5e")
        self._last_clean: dict | None = None

    def physical(self) -> str:
        return f"Roborock {self._host}"

    def details(self) -> dict:
        return {"host": self._host}

    def state(self) -> dict:
        return {"last_clean": self._last_clean}

    async def start(self) -> None:
        self._background("refresh", self.refresh)

    async def refresh(self) -> None:
        """Lit le dernier nettoyage (affiché par le widget)."""
        d = await self._call(self._vacuum.last_clean_details)
        self._last_clean = d and {"start": d.start.isoformat(), "area": round(d.area, 1),
                                  "duration": round(d.duration.total_seconds() / 60),
                                  "complete": d.complete}
        self._notify()

    async def _call(self, func, *args):
        """python-miio est synchrone : exécuté hors de la boucle asyncio."""
        try:
            return await asyncio.to_thread(func, *args)
        except DeviceException as err:
            raise DeviceError(f"{self.id} : {err}") from err

    async def clean(self) -> None:
        await self._call(self._vacuum.start)

    async def pause(self) -> None:
        await self._call(self._vacuum.pause)

    async def home(self) -> None:
        await self._call(self._vacuum.home)

    async def history(self) -> list[dict]:
        """Nettoyages enregistrés : début, fin, durée, surface (m²), terminé ou non."""
        summary = await self._call(self._vacuum.clean_history)
        cleanings = []
        for id_ in summary.ids:
            if d := await self._call(self._vacuum.clean_details, id_):
                cleanings.append({"start": d.start, "end": d.end, "duration": d.duration,
                                  "area": d.area, "complete": d.complete})
        return cleanings

    async def set(self, name: str, value: bool | str | int, transition: int | None = None) -> None:
        if name != "command" or value not in COMMANDS:
            raise ValueError(f"{self.id} ne gère pas « {name}={value} »")
        await getattr(self, value)()


Driver = Roborock  # convention : le chargeur cherche ce nom
