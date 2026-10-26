"""Synchronisations de la page Administration, chacune lancée à la main : un contrôle (y a-t-il
quelque chose à faire ?) puis, au besoin, la synchro.
- app : commits de HabController à tirer -> pull, puis redémarrage (main.py) ;
- download : commits de HabConfig à tirer -> pull ;
- load : HabConfig modifié depuis le dernier chargement -> rechargement (main.py) ;
- save : modifications de HabConfig non commitées ou non poussées -> commit et push.
Un contrôle renvoie {"pending": bool, "text": str} ; une erreur git lève git.GitError."""

import os
import time
from pathlib import Path

from core import git

# écrits par le programme (appairage, cache Python), pas relus au rechargement
IGNORED_DIRS = {".git", "__pycache__", "pairing"}


def state(pending: bool, text: str) -> dict:
    return {"pending": pending, "text": text}


def modified_since(since: float, directory: Path) -> list[str]:
    """Fichiers de la configuration modifiés après la date `since`, et dossiers dont le contenu
    a changé depuis (fichier ajouté, supprimé ou renommé : la date du dossier change)."""
    paths = []
    for top, dirs, names in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        entries = [(Path(top), True)] + [(Path(top, n), False) for n in names
                                         if not n.startswith(".")]  # .gitignore…
        for path, is_dir in entries:
            try:
                if path.stat().st_mtime > since:
                    name = path.relative_to(directory).as_posix()
                    name = directory.name if name == "." else name  # dossier racine
                    paths.append(name + ("/" if is_dir else ""))
            except OSError:  # supprimé entre-temps
                pass
    return sorted(paths)


def describe_files(paths: list[str], shown: int = 3) -> str:
    more = f" et {len(paths) - shown} autre(s)" if len(paths) > shown else ""
    return ", ".join(paths[:shown]) + more


async def check_app(repo: Path, boot_commit: str | None) -> dict:
    """Commits à tirer, ou code déjà tiré mais pas encore redémarré (boot_commit : code en
    marche)."""
    behind = await git.behind(repo)
    if behind:
        return state(True, f"{behind} commit(s) à tirer.")
    if boot_commit and await git.head(repo) != boot_commit:
        return state(True, "Code déjà tiré, redémarrage à faire.")
    return state(False, "À jour.")


async def check_download(repo: Path) -> dict:
    behind = await git.behind(repo)
    if behind:
        return state(True, f"{behind} commit(s) à télécharger.")
    return state(False, "À jour.")


def check_load(directory: Path, loaded_at: float) -> dict:
    loaded = time.strftime("%d/%m %H:%M", time.localtime(loaded_at))
    paths = modified_since(loaded_at, directory)
    if paths:
        return state(True, f"Modifié depuis le chargement du {loaded} : {describe_files(paths)}.")
    return state(False, f"Inchangé depuis le chargement du {loaded}.")


async def check_save(repo: Path) -> dict:
    changes, ahead = await git.pending_backup(repo)
    parts = [f"{changes} fichier(s) modifié(s)" if changes else "",
             f"{ahead} commit(s) non poussé(s)" if ahead else ""]
    if changes or ahead:
        return state(True, ", ".join(p for p in parts if p) + ".")
    return state(False, "Sauvegardé.")
