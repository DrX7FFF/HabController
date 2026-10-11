"""Appaire un équipement HomeKit, enregistre l'appairage et le déclare dans config/devices.toml.

Usage : .venv/bin/python pair.py <id> --driver fp2 [--name "Nom affiché"] [--timeout 10]
Le code HomeKit (XXX-XX-XXX) est saisi sans écho ; redémarrer HabController ensuite.
"""

import argparse
import asyncio
import getpass
import sys

from core.devices import add_device, check_new_device
from core.homekit import ERRORS, PIN, discover, open_controller, pair, pairing_file


def choose(discoveries: list):
    for number, discovery in enumerate(discoveries, start=1):
        description = discovery.description
        state = "déjà appairé" if discovery.paired else "non appairé"
        print(
            f"{number}. {description.name} | {description.id} | "
            f"modèle : {description.model or '?'} | {state}"
        )
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
        check_new_device(args.id, args.driver)
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
        try:
            path = await pair(chosen, args.id, ask_pin())
        except (ValueError, *ERRORS) as err:
            print(f"Échec de l'appairage ({type(err).__name__}).")  # sans détail : rien sur le code
            return 1

    add_device(args.id, args.driver, args.name or args.id)
    print(f"Appairage enregistré dans {path}, équipement « {args.id} » ajouté à devices.toml.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Appairer un équipement HomeKit.")
    parser.add_argument(
        "id", help="identifiant de l'équipement (nom de la section dans devices.toml)"
    )
    parser.add_argument("--driver", required=True, help="pilote à utiliser (drivers/<nom>.py)")
    parser.add_argument("--name", help="nom affiché (défaut : l'identifiant)")
    parser.add_argument("--timeout", type=float, default=10, help="durée de découverte en secondes")
    try:
        return asyncio.run(run(parser.parse_args()))
    except KeyboardInterrupt:
        print("\nInterrompu.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
