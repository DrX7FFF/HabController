"""Synchronisations de la page Administration, chacune lancée à la main : un contrôle (y a-t-il
quelque chose à faire ?) puis, au besoin, la synchro.
- app : commits de HabController à tirer, ou habctrl.toml modifié -> pull, puis redémarrage
  (main.py) ;
- download : commits de HabConfig à tirer -> pull ;
- load : HabConfig modifié depuis le dernier chargement -> rechargement des seules parties
  modifiées (main.py) ;
- save : modifications de HabConfig non commitées ou non poussées -> commit et push.
Un contrôle renvoie {"pending": bool, "text": str} ; une erreur git lève git.GitError."""

import hashlib
import os
import time
from pathlib import Path

from core import git

# Parties de la configuration, rechargées séparément (main.py) : premier élément du chemin dans
# HabConfig -> partie. habctrl.toml : redémarrage (voir check_app). Le reste (pairing/, écrit
# par le programme, README…) n'est pas relu.
PARTS = {
    "devices": "devices",
    "automations": "automations",
    "datalog.toml": "datalog",
    "dashboard": "dashboard",
}
# ce que fait le rechargement d'une partie
ACTIONS = {
    "devices": "rechargement complet, équipements reconnectés",
    "automations": "automatisations",
    "datalog": "datalog",
    "dashboard": "écrans et graphiques",
}


def state(pending: bool, text: str) -> dict:
    return {"pending": pending, "text": text}


def snapshot(directory: Path) -> dict[str, str]:
    """Empreinte du contenu de chaque fichier des parties rechargeables : {chemin : sha256}.
    Le contenu, pas la date : un fichier réécrit à l'identique n'est pas une modification."""
    files = {}
    for top, dirs, names in os.walk(directory):
        dirs[:] = [d for d in dirs if d != "__pycache__" and not d.startswith(".")]
        for name in names:
            path = Path(top, name)
            rel = path.relative_to(directory).as_posix()
            if name.startswith(".") or rel.partition("/")[0] not in PARTS:
                continue
            try:
                files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:  # supprimé entre-temps
                pass
    return files


def changes(loaded: dict[str, str], current: dict[str, str]) -> dict[str, list[str]]:
    """Fichiers ajoutés, supprimés ou modifiés entre deux snapshot(), par partie."""
    parts: dict[str, list[str]] = {}
    for rel in sorted(loaded.keys() | current.keys()):
        if loaded.get(rel) != current.get(rel):
            parts.setdefault(PARTS[rel.partition("/")[0]], []).append(rel)
    return parts


def reload_plan(parts) -> list[str]:
    """Parties à recharger pour les parties modifiées : devices/ l'emporte (rechargement
    complet : tout dépend des équipements)."""
    if "devices" in parts:
        return ["devices"]
    return [part for part in ACTIONS if part in parts]


def describe_files(paths: list[str], shown: int = 3) -> str:
    more = f" et {len(paths) - shown} autre(s)" if len(paths) > shown else ""
    return ", ".join(paths[:shown]) + more


async def check_app(repo: Path, boot_commit: str | None, settings_changed: bool) -> dict:
    """Commits à tirer, ou code déjà tiré mais pas encore redémarré (boot_commit : code en
    marche), ou habctrl.toml modifié depuis le démarrage."""
    behind = await git.behind(repo)
    if behind:
        return state(True, f"{behind} commit(s) à tirer.")
    if boot_commit and await git.head(repo) != boot_commit:
        return state(True, "Code déjà tiré, redémarrage à faire.")
    if settings_changed:
        return state(True, "habctrl.toml modifié, redémarrage à faire.")
    return state(False, "À jour.")


async def check_download(repo: Path) -> dict:
    behind = await git.behind(repo)
    if behind:
        return state(True, f"{behind} commit(s) à télécharger.")
    return state(False, "À jour.")


def check_load(directory: Path, loaded: dict[str, str], loaded_at: float) -> dict:
    """loaded : snapshot() du dernier chargement, fait à la date loaded_at."""
    when = time.strftime("%d/%m %H:%M", time.localtime(loaded_at))
    parts = changes(loaded, snapshot(directory))
    if parts:
        paths = [rel for files in parts.values() for rel in files]
        actions = ", ".join(ACTIONS[part] for part in reload_plan(parts))
        return state(True, f"Modifié depuis le chargement du {when} : {describe_files(paths)} "
                           f"(à recharger : {actions}).")
    return state(False, f"Inchangé depuis le chargement du {when}.")


async def check_save(repo: Path) -> dict:
    changes, ahead = await git.pending_backup(repo)
    parts = [f"{changes} fichier(s) modifié(s)" if changes else "",
             f"{ahead} commit(s) non poussé(s)" if ahead else ""]
    if changes or ahead:
        return state(True, ", ".join(p for p in parts if p) + ".")
    return state(False, "Sauvegardé.")
