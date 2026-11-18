"""Pilote Roborock S5 Max : protocole miio local (UDP 54321), commandes et historique à la demande."""

import asyncio
from datetime import datetime

from miio import DeviceException, RoborockVacuum

from core.base import Device, DeviceError, State, known, spawn_task
from core.datalog import device_path, record

HISTORY_PERIOD = 3600  # s, le robot garde ses 20 derniers nettoyages


class Roborock(Device):
    """Robot aspirateur : le dernier nettoyage est gardé, le reste interroge le robot."""

    WIDGET = "vacuum"
    last_clean = State[dict]()  # début, surface, durée, terminé ; lu par refresh()

    def __init__(self, host: str, token: str):
        super().__init__()
        self._host = host
        self._vacuum = RoborockVacuum(host, token, model="roborock.vacuum.s5e")
        self._logged: set[tuple[datetime, datetime]] = set()  # plages déjà envoyées au datalog
        self._history_task: asyncio.Task | None = None

    def physical(self) -> str:
        return f"Roborock {self._host}"

    def details(self) -> dict:
        return {"host": self._host}

    def state(self) -> dict:
        return known(last_clean=self.last_clean)

    async def start(self) -> None:
        self.refresh()
        self._history_task = spawn_task(self._history_loop(), f"{self.id}.history")

    async def stop(self) -> None:
        if self._history_task:
            self._history_task.cancel()

    async def _history_loop(self) -> None:
        while True:
            self._background("history", self.log_history)
            await asyncio.sleep(HISTORY_PERIOD)

    async def log_history(self) -> None:
        """Plages de nettoyage dans le datalog : devices/<id>/on, true au début, false à la fin."""
        path = device_path(self.id, "on")
        for c in await self.history():
            if (c["start"], c["end"]) not in self._logged:
                record(path, True, at=int(c["start"].timestamp() * 1000))
                record(path, False, at=int(c["end"].timestamp() * 1000))
                self._logged.add((c["start"], c["end"]))

    def refresh(self) -> None:
        """Relit le dernier nettoyage (affiché par le widget)."""
        self._background("refresh", self._refresh)

    async def _refresh(self) -> None:
        d = await self._call(self._vacuum.last_clean_details)
        Roborock.last_clean.update(self, d and {
            "start": d.start.isoformat(), "area": round(d.area, 1),
            "duration": round(d.duration.total_seconds() / 60), "complete": d.complete,
        })

    async def _call(self, func, *args):
        """python-miio est synchrone : exécuté hors de la boucle asyncio."""
        try:
            return await asyncio.to_thread(func, *args)
        except DeviceException as err:
            raise DeviceError(f"{self.id} : {err}") from err

    def clean(self) -> None:
        self._background("clean", lambda: self._call(self._vacuum.start))

    def pause(self) -> None:
        self._background("pause", lambda: self._call(self._vacuum.pause))

    def home(self) -> None:
        self._background("home", lambda: self._call(self._vacuum.home))

    async def history(self) -> list[dict]:
        """Nettoyages enregistrés : début, fin, durée, surface (m²), terminé ou non."""
        summary = await self._call(self._vacuum.clean_history)
        cleanings = []
        for id_ in summary.ids:
            if d := await self._call(self._vacuum.clean_details, id_):
                cleanings.append({"start": d.start, "end": d.end, "duration": d.duration,
                                  "area": d.area, "complete": d.complete})
        return cleanings

    async def last_complete(self) -> datetime | None:
        """Début du dernier nettoyage terminé, None s'il n'y en a aucun dans l'historique."""
        return max((c["start"] for c in await self.history() if c["complete"]), default=None)


Driver = Roborock  # convention : le chargeur cherche ce nom
