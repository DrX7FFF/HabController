"""Appairage : ./habctrl pair <protocole> <id> [options du protocole].

Chaque protocole qui sait appairer fournit drivers/<protocole>/pair.py avec :
HELP (une ligne), add_arguments(parser) pour ses options et `async run(args) -> int`.
Redémarrer HabController ensuite.
"""

import argparse
import asyncio
import importlib
import importlib.util
import pkgutil
import sys

import drivers


def protocols() -> dict:
    """Protocoles qui ont un module d'appairage : {nom: module}."""
    found = {}
    for info in pkgutil.iter_modules(drivers.__path__):
        if info.ispkg and importlib.util.find_spec(f"drivers.{info.name}.pair"):
            found[info.name] = importlib.import_module(f"drivers.{info.name}.pair")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="habctrl pair", description="Appairer un équipement.")
    commands = parser.add_subparsers(dest="protocol", required=True, metavar="protocole")
    for name, module in sorted(protocols().items()):
        command = commands.add_parser(name, help=module.HELP, description=module.HELP)
        command.add_argument(
            "id", help="identifiant de l'équipement (nom du fichier devices/<id>.py)"
        )
        module.add_arguments(command)
        command.set_defaults(run=module.run)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(args.run(args))
    except KeyboardInterrupt:
        print("\nInterrompu.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
