"""Appairage : ./habctrl pair <protocole> [--duration N].

Le déroulé est commun à tous les protocoles (classe Pairing) :
1. ouvrir la session du protocole (clé Zigbee, contrôleur HomeKit…) et chercher pendant N s ;
2. choisir l'appareil parmi ceux trouvés et le reconnaître par son identité stable : déjà lié à
   un équipement logique, on propose de garder ce nom ; sinon on demande le nom de l'équipement,
   dont l'identifiant est déduit. Un identifiant déjà lié à un autre appareil est remplacé après
   confirmation (appareil HS) ; un équipement déjà déclaré dans devices/ est gardé et rattaché ;
3. lier l'appareil (pairing/) ;
4. équipement pas encore déclaré : il est ajouté à devices/devices.py avec le pilote du modèle
   (inconnu : à la main).

Chaque protocole qui sait appairer fournit drivers/<protocole>/pair.py avec une classe
`Pairing` dérivée de celle-ci. Redémarrer HabController ensuite.
"""

import argparse
import asyncio
import importlib
import importlib.util
import pkgutil
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import drivers
from core import check_config
from core.base import Device
from core.devices import add_device, check_id, driver_for_model, id_from_name, load_devices


@dataclass
class Candidate:
    """Appareil physique trouvé par la recherche."""

    uid: object  # identité stable : IEEE, identifiant HomeKit…
    models: list[str]  # clés cherchées dans models.toml, de la plus précise à la plus générale
    comment: str  # au-dessus de la déclaration
    details: list[str]  # affichées pour le choisir
    device: object  # objet du protocole


async def ask(prompt: str) -> str:
    """input() sans bloquer la boucle asyncio : la session du protocole reste vivante."""
    return (await asyncio.to_thread(input, prompt)).strip()


class Pairing:
    """Déroulé commun ; un protocole surcharge les méthodes de la partie « à fournir ».

    PROTOCOL est le paquet du protocole (drivers/<protocole>/) : il fournit ERRORS et
    open_session() (contexte asynchrone : clé, contrôleur…), et sa table modèle → pilote
    models.toml. Par défaut, un lien est un fichier par équipement : PROTOCOL fournit alors
    paired_ids(uid) et pairing_file(id).
    """

    PROTOCOL: ModuleType
    HELP = ""
    DURATION = 10  # s, recherche par défaut
    MAX_DURATION = 600
    HINT = ""  # conseil affiché avec une erreur
    SECRET_ERRORS = False  # erreurs affichées sans leur texte (il pourrait contenir un code)
    previous = ""  # équipement logique auquel l'appareil était lié, s'il en change
    declared: dict[str, Device] = {}  # équipements déclarés dans devices/

    @property
    def models(self) -> Path:
        return Path(self.PROTOCOL.__file__).with_name("models.toml")

    # --- à fournir par le protocole -----------------------------------------

    async def search(self, session, duration: int) -> list[Candidate]:
        raise NotImplementedError

    async def link(self, session, candidate: Candidate, dev_id: str) -> Path:
        """Appaire l'appareil et enregistre son lien avec `dev_id` (remplace l'ancien)."""
        raise NotImplementedError

    def paired_ids(self, uid) -> list[str]:
        """Équipements logiques liés à cet appareil."""
        return self.PROTOCOL.paired_ids(uid)

    def linked(self, dev_id: str) -> bool:
        """L'équipement logique est-il lié à un appareil ?"""
        return self.PROTOCOL.pairing_file(dev_id).exists()

    def unlink(self, dev_id: str) -> None:
        self.PROTOCOL.pairing_file(dev_id).unlink(missing_ok=True)

    def done(self) -> None:
        """Session fermée, appairage enregistré."""

    # --- déroulé -------------------------------------------------------------

    async def run(self, duration: int) -> int:
        self.declared = load_devices()
        try:
            async with self.PROTOCOL.open_session() as session:
                candidates = await self.search(session, duration)
                if not candidates:
                    print("Aucun équipement trouvé.")
                    return 1
                candidate = await self.choose(candidates)
                dev_id, name = await self.target(candidate.uid)
                path = await self.link(session, candidate, dev_id)
        except self.PROTOCOL.ERRORS as err:
            detail = "" if self.SECRET_ERRORS else f" : {err}"
            print(f"Échec ({type(err).__name__}{detail}). {self.HINT}")
            return 1
        self.done()
        print(f"Appairage de « {dev_id} » enregistré dans {path}.")
        if self.previous and self.previous != dev_id:
            self.unlink(self.previous)
            print(f"« {self.previous} » n'a plus d'appareil : le retirer de devices/.")
        if dev_id in self.declared:
            print(f"« {dev_id} » déjà déclaré dans devices/, inchangé.")
            return 0
        driver = driver_for_model(candidate.models, self.models)
        if not driver:
            print(
                f"\nModèle inconnu : déclarer « {dev_id} » dans devices/ avec le bon pilote, "
                f"ou l'ajouter à {self.models}, puis réappairer."
            )
            return 0
        try:
            file = add_device(dev_id, driver, name=name, comment=candidate.comment)
        except ValueError as err:
            print(f"Équipement non ajouté : {err}")
            return 1
        print(f"Équipement « {name or dev_id} » ajouté à {file}.")
        return 0

    async def choose(self, candidates: list[Candidate]) -> Candidate:
        for number, candidate in enumerate(candidates, start=1):
            print(f"\n{number}. " + "\n   ".join(candidate.details))
        print()
        while len(candidates) > 1:
            answer = await ask("Numéro de l'équipement à appairer : ")
            if answer.isdigit() and 1 <= int(answer) <= len(candidates):
                return candidates[int(answer) - 1]
            print("Choix invalide.")
        return candidates[0]

    async def target(self, uid) -> tuple[str, str]:
        """(id, nom) de l'équipement logique ; nom vide pour un équipement déjà connu."""
        for dev_id in self.paired_ids(uid):
            label = self.label(dev_id)
            answer = await ask(f"Équipement reconnu, anciennement {label}. Garder ce nom ? [O/n] ")
            if answer.lower() not in ("n", "non"):
                return dev_id, ""
            self.previous = dev_id
        while True:
            name = await ask("Nom de l'équipement (ex. Prise salon) : ")
            dev_id = id_from_name(name)
            try:
                check_id(dev_id)
            except ValueError as err:
                print(f"Erreur : {err}")
                continue
            if dev_id != self.previous and self.linked(dev_id):
                answer = await ask(
                    f"{self.label(dev_id)} est lié à un autre appareil : le remplacer ? [o/N] "
                )
                if answer.lower() not in ("o", "oui"):
                    continue
            return dev_id, "" if dev_id in self.declared else name

    def label(self, dev_id: str) -> str:
        """« Nom (id) » d'un équipement déclaré, ou l'identifiant seul."""
        device = self.declared.get(dev_id)
        return f"{device.name} ({dev_id})" if device else dev_id


def protocols() -> dict:
    """Protocoles qui savent appairer : {nom: classe Pairing}."""
    found = {}
    for info in pkgutil.iter_modules(drivers.__path__):
        if info.ispkg and importlib.util.find_spec(f"drivers.{info.name}.pair"):
            found[info.name] = importlib.import_module(f"drivers.{info.name}.pair").Pairing
    return found


def duration_type(maximum: int):
    def duration(text: str) -> int:
        value = int(text)
        if not 1 <= value <= maximum:
            raise argparse.ArgumentTypeError(f"entre 1 et {maximum} secondes")
        return value

    return duration


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="habctrl pair", description="Appairer un équipement.")
    commands = parser.add_subparsers(dest="protocol", required=True, metavar="protocole")
    for name, cls in sorted(protocols().items()):
        command = commands.add_parser(name, help=cls.HELP, description=cls.HELP)
        command.add_argument(
            "--duration",
            type=duration_type(cls.MAX_DURATION),
            default=cls.DURATION,
            help=f"durée de recherche en secondes (défaut {cls.DURATION})",
        )
        command.set_defaults(pairing=cls)
    args = parser.parse_args(argv)
    check_config()
    try:
        return asyncio.run(args.pairing().run(args.duration))
    except KeyboardInterrupt:
        print("\nInterrompu.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
