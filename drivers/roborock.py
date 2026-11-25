"""Pilote Roborock S5 Max : protocole miio local (UDP 54321), commandes et historique à la
demande."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import override

from miio.exceptions import DeviceException
from miio.integrations.vacuum.roborock.vacuum import RoborockVacuum
from miio.integrations.vacuum.roborock.vacuumcontainers import CleaningDetails

from core.base import DeviceError, IPDevice, State, actionweb
from core.datalog import device_path, record

HISTORY_PERIOD = 3600  # s, le robot garde ses 20 derniers nettoyages


class Roborock(IPDevice):
    """Robot aspirateur : le dernier nettoyage est gardé, le reste interroge le robot."""

    WIDGET = "vacuum"
    BRAND = "Roborock"
    last_clean = State[dict]()  # début, surface, durée, terminé ; lu par refresh()

    def __init__(self, host: str, token: str):
        super().__init__(host)
        self._vacuum = RoborockVacuum(host, token, model="roborock.vacuum.s5e")
        self._logged: set[tuple[datetime, datetime]] = set()  # plages déjà envoyées au datalog

    @override
    async def start(self) -> None:
        self.refresh()
        self._spawn(self._history_loop())

    async def _history_loop(self) -> None:
        while True:
            self._background("history", self.log_history)
            await asyncio.sleep(HISTORY_PERIOD)

    async def log_history(self) -> None:
        """Plages de nettoyage dans le datalog : devices/<id>/on, true au début, false à la fin."""
        path = device_path(self.id, "on")
        for c in await self.history():
            if (c.start, c.end) not in self._logged:
                record(path, True, at=int(c.start.timestamp() * 1000))
                record(path, False, at=int(c.end.timestamp() * 1000))
                self._logged.add((c.start, c.end))

    @actionweb
    def refresh(self) -> None:
        """Relit le dernier nettoyage (affiché par le widget)."""
        self._background("refresh", self._refresh)

    async def _refresh(self) -> None:
        d = await self._call(self._vacuum.last_clean_details)
        Roborock.last_clean.update(self, d and {
            "start": d.start.isoformat(), "area": round(d.area, 1),
            "duration": round(d.duration.total_seconds() / 60), "complete": d.complete,
        })

    async def _call[R](self, func: Callable[..., R], *args: object) -> R:
        """python-miio est synchrone : exécuté hors de la boucle asyncio."""
        try:
            return await asyncio.to_thread(func, *args)
        except DeviceException as err:
            raise DeviceError(f"{self.id} : {err}") from err

    @actionweb
    def clean(self) -> None:
        self._background("clean", lambda: self._call(self._vacuum.start))

    @actionweb
    def pause(self) -> None:
        self._background("pause", lambda: self._call(self._vacuum.pause))

    @actionweb
    def home(self) -> None:
        self._background("home", lambda: self._call(self._vacuum.home))

    async def history(self) -> list[CleaningDetails]:
        """Nettoyages gardés par le robot : start, end, duration, area (m²), complete."""
        summary = await self._call(self._vacuum.clean_history)
        return [
            d
            for id_ in summary.ids
            if isinstance(d := await self._call(self._vacuum.clean_details, id_), CleaningDetails)
        ]

    async def last_complete(self) -> datetime | None:
        """Début du dernier nettoyage terminé, None s'il n'y en a aucun dans l'historique."""
        return max((c.start for c in await self.history() if c.complete), default=None)
