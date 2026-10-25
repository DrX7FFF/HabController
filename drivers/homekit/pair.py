"""Appairage HomeKit."""

from dataclasses import fields
from pathlib import Path

from core import pair
from drivers import homekit
from drivers.homekit import PIN, discover, pair_accessory


class Pairing(pair.Pairing):
    PROTOCOL = homekit
    LABEL = "HomeKit"
    SECRET_ERRORS = True

    async def search(self, controller, duration: int) -> list[pair.Candidate]:
        self.ui.say(f"Recherche des accessoires HomeKit pendant {duration} s…")
        self.ui.wait(duration)
        try:
            discoveries = await discover(controller, duration)
        finally:
            self.ui.wait(0)
        candidates = []
        for discovery in discoveries:
            desc = discovery.description
            if discovery.paired:
                self.ui.say(
                    f"{desc.name} : déjà appairé, ignoré (le réinitialiser pour l'appairer)."
                )
                continue
            candidates.append(
                pair.Candidate(
                    uid=desc.id.lower(),
                    models=[desc.model],
                    comment=f"HomeKit : {desc.name}, modèle {desc.model}",
                    details=[f"{f.name} : {getattr(desc, f.name)}" for f in fields(desc)],
                    device=discovery,
                )
            )
        return candidates

    async def link(self, controller, candidate: pair.Candidate, dev_id: str) -> Path:
        return await pair_accessory(candidate.device, dev_id, await self.ask_pin())

    async def ask_pin(self) -> str:
        while True:
            pin = await self.ui.ask("Code HomeKit (XXX-XX-XXX)", secret=True)
            if PIN.fullmatch(pin):
                return pin
            self.ui.say("Format invalide.")
