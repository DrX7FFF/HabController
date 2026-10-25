"""Appairage d'un équipement, déroulé commun à tous les protocoles.

Le dialogue passe par l'objet `ui` (voir Dialog), fourni par l'appelant.
Chaque protocole qui sait appairer fournit drivers/<protocole>/pair.py avec une classe
`Pairing` dérivée de celle-ci."""

import importlib
import importlib.util
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol

import drivers
from core.base import Device
from core.devices import add_device, check_id, driver_for_model, id_from_name, load_devices

YES_NO = [("o", "Oui"), ("n", "Non")]


class Dialog(Protocol):
    """Échange avec la personne qui appaire."""

    def say(self, text: str) -> None: ...

    def wait(self, seconds: int) -> None:
        """Attente de `seconds` s qui commence (recherche) ; 0 : attente terminée."""

    async def ask(
        self, prompt: str, choices: list[tuple[str, str]] | None = None, secret: bool = False
    ) -> str:
        """Réponse saisie (sans espaces autour) ; `choices` : réponses proposées (valeur,
        libellé) ; `secret` : saisie masquée."""
        ...


@dataclass
class Candidate:
    """Appareil physique trouvé par la recherche."""

    uid: object  # identité stable : IEEE, identifiant HomeKit…
    models: list[str]  # clés cherchées dans models.toml, de la plus précise à la plus générale
    comment: str  # au-dessus de la déclaration
    details: list[str]  # affichées pour le choisir
    device: object  # objet du protocole
    args: tuple[str, ...] = ()  # arguments de la déclaration propres à l'appareil (ex. adresse)


class Pairing:
    """Déroulé commun ; un protocole surcharge les méthodes de la partie « à fournir ».

    PROTOCOL est le paquet du protocole (drivers/<protocole>/) : il fournit ERRORS et
    open_session() (contexte asynchrone : clé, contrôleur…), et sa table modèle → pilote
    models.toml. Par défaut, un lien est un fichier par équipement : PROTOCOL fournit alors
    paired_ids(uid) et pairing_file(id). Un protocole dont le lien est porté par la déclaration
    (arguments de Candidate) n'enregistre rien : link() renvoie None.
    """

    PROTOCOL: ModuleType
    LABEL = ""  # nom affiché du protocole
    DURATION = 10  # s, recherche par défaut
    MAX_DURATION = 600
    HINT = ""  # conseil affiché avec une erreur
    SECRET_ERRORS = False  # erreurs affichées sans leur texte (il pourrait contenir un code)
    CLEANUP = False  # sait retrouver et retirer les appareils inutilisés (unused, remove_unused)
    previous = ""  # équipement logique auquel l'appareil était lié, s'il en change
    declared: dict[str, Device] = {}  # équipements déclarés dans devices/

    def __init__(self, ui: Dialog):
        self.ui = ui

    @property
    def models(self) -> Path:
        return Path(self.PROTOCOL.__file__).with_name("models.toml")

    # --- à fournir par le protocole -----------------------------------------

    async def search(self, session, duration: int) -> list[Candidate]:
        raise NotImplementedError

    async def link(self, session, candidate: Candidate, dev_id: str) -> Path | None:
        """Appaire l'appareil et enregistre son lien avec `dev_id` (remplace l'ancien) ; renvoie
        le fichier du lien, None si la déclaration le porte."""
        raise NotImplementedError

    def paired_ids(self, uid) -> list[str]:
        """Équipements logiques liés à cet appareil."""
        return self.PROTOCOL.paired_ids(uid)

    def linked(self, dev_id: str) -> bool:
        """L'équipement logique est-il lié à un appareil ?"""
        return self.PROTOCOL.pairing_file(dev_id).exists()

    def unlink(self, dev_id: str) -> None:
        self.PROTOCOL.pairing_file(dev_id).unlink(missing_ok=True)

    def session(self):
        """Contexte asynchrone de la session du protocole (clé, contrôleur…)."""
        return self.PROTOCOL.open_session()

    def done(self) -> None:
        """Session fermée, appairage enregistré."""

    @classmethod
    async def unused(cls) -> list[str]:
        """Appareils appairés qu'aucun équipement déclaré n'utilise (description de chacun)."""
        raise NotImplementedError

    @classmethod
    async def remove_unused(cls) -> list[str]:
        """Désappaire les appareils inutilisés ; renvoie la description des retirés."""
        raise NotImplementedError

    # --- déroulé -------------------------------------------------------------

    async def run(self, duration: int) -> bool:
        """Déroulé complet ; True si l'appairage est enregistré."""
        say = self.ui.say
        self.declared = load_devices()
        try:
            async with self.session() as session:
                candidates = await self.search(session, duration)
                if not candidates:
                    say("Aucun équipement trouvé.")
                    return False
                candidate = await self.choose(candidates)
                dev_id = await self.target(candidate.uid)
                path = await self.link(session, candidate, dev_id)
        except self.PROTOCOL.ERRORS as err:
            detail = "" if self.SECRET_ERRORS else f" : {err}"
            say(f"Échec ({type(err).__name__}{detail}). {self.HINT}")
            return False
        self.done()
        say(f"Appairage de « {dev_id} » enregistré" + (f" dans {path}." if path else "."))
        if self.previous and self.previous != dev_id:
            self.unlink(self.previous)
            say(f"« {self.previous} » n'a plus d'appareil : le retirer de devices/.")
        if dev_id in self.declared:
            say(f"« {dev_id} » déjà déclaré dans devices/, inchangé.")
        elif not (driver := driver_for_model(candidate.models, self.models)):
            args = f" et {', '.join(candidate.args)}" if candidate.args else ""
            say(
                f"Modèle inconnu : déclarer « {dev_id} » dans devices/ avec le bon pilote{args}, "
                f"ou l'ajouter à {self.models}, puis réappairer."
            )
        else:
            try:
                file = add_device(dev_id, driver, comment=candidate.comment, args=candidate.args)
            except ValueError as err:
                say(f"Équipement non ajouté : {err}")
                return False
            say(f"Équipement « {dev_id} » ajouté à {file}.")
        say("Redémarrer HabController pour l'utiliser.")
        return True

    async def choose(self, candidates: list[Candidate]) -> Candidate:
        for number, candidate in enumerate(candidates, start=1):
            self.ui.say(f"{number}. " + "\n   ".join(candidate.details))
        numbers = [(str(n), str(n)) for n in range(1, len(candidates) + 1)]
        while len(candidates) > 1:
            answer = await self.ui.ask("Numéro de l'équipement à appairer", numbers)
            if answer.isdigit() and 1 <= int(answer) <= len(candidates):
                return candidates[int(answer) - 1]
            self.ui.say("Choix invalide.")
        return candidates[0]

    async def target(self, uid) -> str:
        """Identifiant de l'équipement logique."""
        for dev_id in self.paired_ids(uid):
            answer = await self.ui.ask(
                f"Équipement reconnu, anciennement « {dev_id} ». Garder cet identifiant ?", YES_NO
            )
            if answer.lower() not in ("n", "non"):
                return dev_id
            self.previous = dev_id
        while True:
            dev_id = await self.ask_id()
            if dev_id != self.previous and self.linked(dev_id):
                answer = await self.ui.ask(
                    f"« {dev_id} » est lié à un autre appareil : le remplacer ?", YES_NO
                )
                if answer.lower() not in ("o", "oui"):
                    continue
            return dev_id

    async def ask_id(self) -> str:
        """Identifiant saisi, redemandé tant qu'il est invalide."""
        while True:
            dev_id = id_from_name(await self.ui.ask("Identifiant de l'équipement (ex. prise_salon)"))
            try:
                check_id(dev_id)
            except ValueError as err:
                self.ui.say(f"Erreur : {err}")
                continue
            return dev_id


def protocols() -> dict:
    """Protocoles qui savent appairer : {nom: classe Pairing}."""
    found = {}
    for info in pkgutil.iter_modules(drivers.__path__):
        if info.ispkg and importlib.util.find_spec(f"drivers.{info.name}.pair"):
            found[info.name] = importlib.import_module(f"drivers.{info.name}.pair").Pairing
    return found
