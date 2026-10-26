"""Surveillances, sources de notifications (core/notifications.py) :
- configuration : fichiers de HabConfig modifiés, ajoutés ou supprimés depuis leur chargement ;
- sauvegarde : modifications de HabConfig pas encore commitées ou poussées ;
- mise à jour : commits de HabController à tirer, ou code tiré pas encore redémarré.

Réglages ([watch] de habctrl.toml) : check (s, défaut 30) pour la configuration et la
sauvegarde, fetch (s, défaut 3600) pour la recherche d'une mise à jour (réseau)."""

import asyncio
import logging
import os
import time
from pathlib import Path

from core import CONFIG_DIR, ROOT, git, settings
from core.notifications import Notifications

log = logging.getLogger(__name__)

# écrits par le programme (appairage, cache Python), pas relus au rechargement
IGNORED_DIRS = {".git", "__pycache__", "pairing"}
CONFIG, BACKUP, UPDATE = "config", "backup", "update"  # identifiants des notifications


def config_files(directory: Path = CONFIG_DIR) -> dict[str, int]:
    """{chemin relatif : date de modification (ns)} des fichiers de la configuration."""
    files = {}
    for top, dirs, names in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for name in names:
            if name.startswith("."):  # .gitignore…
                continue
            path = Path(top, name)
            try:
                files[path.relative_to(directory).as_posix()] = path.stat().st_mtime_ns
            except OSError:  # supprimé entre-temps
                pass
    return files


def changed(before: dict[str, int], after: dict[str, int]) -> list[str]:
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def describe_files(paths: list[str], shown: int = 3) -> str:
    more = f" et {len(paths) - shown} autre(s)" if len(paths) > shown else ""
    return ", ".join(paths[:shown]) + more


class Watch:
    def __init__(self, notifications: Notifications, config_dir: Path = CONFIG_DIR,
                 code_dir: Path = ROOT):
        self.notifications = notifications
        self.config_dir = config_dir
        self.code_dir = code_dir
        self.loaded: dict[str, int] = {}  # état des fichiers au dernier chargement
        self.loaded_at: float | None = None
        self.boot_commit: str | None = None  # code en marche
        self.behind = 0
        self.errors: dict[str, str] = {}  # dernière erreur par surveillance (journal sans répétition)

    def snapshot(self) -> dict[str, int]:
        """À prendre juste avant un chargement, puis à passer à mark_loaded() s'il réussit."""
        return config_files(self.config_dir)

    def mark_loaded(self, files: dict[str, int]) -> None:
        self.loaded, self.loaded_at = files, time.time()
        self.check_config()

    def _failed(self, key: str, error: Exception | None) -> None:
        """Journalise une erreur de surveillance à son apparition et à sa disparition."""
        text = str(error) if error else None
        if text != self.errors.get(key):
            if text:
                log.warning("Surveillance %s impossible : %s", key, text)
            elif key in self.errors:
                log.info("Surveillance %s rétablie", key)
        if text:
            self.errors[key] = text
        else:
            self.errors.pop(key, None)

    def check_config(self) -> None:
        if self.loaded_at is None:
            return
        paths = changed(self.loaded, config_files(self.config_dir))
        if paths:
            loaded = time.strftime("%d/%m %H:%M", time.localtime(self.loaded_at))
            self.notifications.set(CONFIG, f"Configuration modifiée depuis son chargement "
                                   f"({loaded}) : {describe_files(paths)}. À recharger.")
        else:
            self.notifications.clear(CONFIG)

    async def check_backup(self) -> None:
        try:
            changes, ahead = await git.pending_backup(self.config_dir)
        except git.GitError as err:
            self._failed(BACKUP, err)
            self.notifications.set(BACKUP, f"État de la sauvegarde de HabConfig illisible "
                                   f"({err}).", "error")
            return
        self._failed(BACKUP, None)
        parts = [f"{changes} fichier(s) modifié(s)" if changes else "",
                 f"{ahead} commit(s) non poussé(s)" if ahead else ""]
        if changes or ahead:
            self.notifications.set(BACKUP, "HabConfig : " + ", ".join(p for p in parts if p)
                                   + ". Sauvegarde à faire.")
        else:
            self.notifications.clear(BACKUP)

    async def check_update(self, fetch: bool = True) -> None:
        try:
            head = await git.head(self.code_dir)
            if self.boot_commit is None:
                self.boot_commit = head
            self.behind = await git.behind(self.code_dir, fetch)
        except git.GitError as err:
            self._failed(UPDATE, err)  # réseau absent : pas de notification
            return
        self._failed(UPDATE, None)
        if self.behind:
            self.notifications.set(UPDATE, f"Mise à jour de HabController disponible "
                                   f"({self.behind} commit(s)).")
        elif head != self.boot_commit:
            self.notifications.set(UPDATE, "HabController mis à jour, redémarrage à faire.")
        else:
            self.notifications.clear(UPDATE)

    async def run(self) -> None:
        conf = settings("watch")
        check, fetch = conf.get("check", 30), conf.get("fetch", 3600)
        last_fetch = None
        while True:
            self.check_config()
            await self.check_backup()
            if last_fetch is None or time.monotonic() - last_fetch >= fetch:
                last_fetch = time.monotonic()
                await self.check_update()
            await asyncio.sleep(check)
