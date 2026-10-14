"""Appairage HomeKit (déroulé commun : core/pair.py).

La découverte mDNS écoute pendant `--duration` secondes ; les accessoires déjà appairés sont
ignorés (les réinitialiser pour les appairer). L'accessoire est reconnu par son identifiant
HomeKit (ex. après une réinitialisation). Le code HomeKit (XXX-XX-XXX) est saisi sans écho.
"""

import asyncio
import getpass
from dataclasses import fields
from pathlib import Path

from core import pair
from drivers import homekit
from drivers.homekit import PIN, discover, pair_accessory


async def ask_pin() -> str:
    while True:
        pin = await asyncio.to_thread(getpass.getpass, "Code HomeKit (XXX-XX-XXX) : ")
        if PIN.fullmatch(pin):
            return pin
        print("Format invalide.")


class Pairing(pair.Pairing):
    PROTOCOL = homekit
    HELP = "appairer un accessoire HomeKit (découverte mDNS, code XXX-XX-XXX)"
    SECRET_ERRORS = True

    async def search(self, controller, duration: int) -> list[pair.Candidate]:
        print(f"Recherche des accessoires HomeKit pendant {duration} s…")
        candidates = []
        for discovery in await discover(controller, duration):
            desc = discovery.description
            if discovery.paired:
                print(f"{desc.name} : déjà appairé, ignoré (le réinitialiser pour l'appairer).")
                continue
            candidates.append(
                pair.Candidate(
                    uid=desc.id.lower(),
                    model=desc.model,
                    comment=f"HomeKit : {desc.name}, modèle {desc.model}",
                    details=[f"{f.name} : {getattr(desc, f.name)}" for f in fields(desc)],
                    device=discovery,
                )
            )
        return candidates

    async def link(self, controller, candidate: pair.Candidate, dev_id: str) -> Path:
        return await pair_accessory(candidate.device, dev_id, await ask_pin())
