"""Appaire un équipement HomeKit, enregistre l'appairage et crée devices/<id>.py.

Usage : .venv/bin/python pair.py <id> [--timeout 10]
L'équipement est ajouté sans nom ; son pilote est déduit du modèle (drivers/homekit_models.toml),
sinon aucun fichier n'est créé : l'équipement est à déclarer à la main dans devices/.
Le code HomeKit (XXX-XX-XXX) est saisi sans écho.
Redémarrer HabController ensuite.
"""

import argparse
import asyncio
import dataclasses
import getpass
import sys

from core import ROOT
from core.devices import add_device, check_new_device, driver_for_model
from core.homekit import ERRORS, PIN, discover, open_controller, pair, pairing_file

MODELS = ROOT / "drivers" / "homekit_models.toml"


def choose(discoveries: list):
    """Affiche tout ce que la découverte a vu, puis fait choisir un accessoire."""
    for number, discovery in enumerate(discoveries, start=1):
        state = "déjà appairé" if discovery.paired else "non appairé"
        print(f"\n{number}. {discovery.description.name} [{state}]")
        for field in dataclasses.fields(discovery.description):
            print(f"     {field.name} : {getattr(discovery.description, field.name)}")
    print()
    while True:
        answer = input("Numéro de l'accessoire à appairer : ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(discoveries):
            return discoveries[int(answer) - 1]
        print("Choix invalide.")


def ask_pin() -> str:
    while True:
        pin = getpass.getpass("Code HomeKit (XXX-XX-XXX) : ")
        if PIN.fullmatch(pin):
            return pin
        print("Format invalide.")


async def run(args: argparse.Namespace) -> int:
    try:
        check_new_device(args.id)
        if pairing_file(args.id).exists():
            raise ValueError(
                f"un appairage existe déjà pour « {args.id} » ({pairing_file(args.id)})"
            )
    except ValueError as err:
        print(f"Erreur : {err}")
        return 1

    async with open_controller() as controller:
        print(f"Recherche des équipements HomeKit pendant {args.timeout:g} s…")
        discoveries = await discover(controller, args.timeout)
        if not discoveries:
            print("Aucun équipement HomeKit trouvé sur le réseau.")
            return 1
        chosen = choose(discoveries)
        if chosen.paired:
            print("Cet équipement est déjà appairé : le réinitialiser avant de recommencer.")
            return 1
        description = chosen.description
        driver = driver_for_model(
            description.model, MODELS
        )  # avant l'appairage : rien ne doit échouer après
        try:
            path = await pair(chosen, args.id, ask_pin())
        except (ValueError, *ERRORS) as err:
            print(f"Échec de l'appairage ({type(err).__name__}).")  # sans détail : rien sur le code
            return 1

    print(f"Appairage enregistré dans {path}.")
    if not driver:
        print(
            f"Modèle inconnu : déclarer l'équipement dans devices/{args.id}.py avec le bon pilote."
        )
        return 0
    try:
        add_device(
            args.id, driver, comment=f"HomeKit : {description.name}, modèle {description.model}"
        )
    except ValueError as err:
        print(f"Équipement non ajouté : {err}")
        return 1
    print(f"Équipement « {args.id} » créé : devices/{args.id}.py.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Appairer un équipement HomeKit.")
    parser.add_argument(
        "id", help="identifiant de l'équipement (nom du fichier devices/<id>.py)"
    )
    parser.add_argument("--timeout", type=float, default=10, help="durée de découverte en secondes")
    try:
        return asyncio.run(run(parser.parse_args()))
    except KeyboardInterrupt:
        print("\nInterrompu.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
