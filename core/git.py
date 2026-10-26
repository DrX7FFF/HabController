"""Commandes git sur les dépôts HabConfig (sauvegarde) et HabController (mise à jour).

Sans terminal : un dépôt distant qui demande un mot de passe fait échouer la commande au lieu
de la bloquer (identifiants à enregistrer sur la machine : clé SSH ou credential helper)."""

import asyncio
import os
from datetime import datetime
from pathlib import Path

TIMEOUT = 120  # s, par commande

# identité du commit si la machine n'en a pas (git config user.name / user.email)
IDENTITY = ("-c", "user.name=HabController", "-c", "user.email=habctrl@localhost")


class GitError(Exception):
    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output  # sortie complète de git


def _summary(output: str) -> str:
    """Ligne utile d'une sortie de git en erreur : la première « error: » ou « fatal: »,
    sinon la dernière qui n'est pas un conseil (« hint: »)."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in lines:
        if line.startswith(("error:", "fatal:")):
            return line.partition(":")[2].strip()
    lines = [line for line in lines if not line.startswith("hint:")]
    return lines[-1] if lines else ""


def _env() -> dict:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


async def git(repo: Path, *args: str, timeout: float = TIMEOUT) -> str:
    """Sortie de la commande ; GitError si elle échoue (message de git) ou ne répond pas."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(repo), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_env(),
        )
    except OSError as err:  # git absent
        raise GitError(f"git introuvable ({err})") from err
    name = next(a for a in args if not a.startswith("-") and "=" not in a)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitError(f"git {name} : pas de réponse après {timeout:.0f} s") from None
    if proc.returncode:
        output = (out + err).decode(errors="replace")
        raise GitError(f"git {name} : {_summary(output) or proc.returncode}", output)
    return out.decode(errors="replace").strip()


async def _count(repo: Path, revisions: str) -> int:
    return int(await git(repo, "rev-list", "--count", revisions))


async def pending_backup(repo: Path) -> tuple[int, int]:
    """(fichiers modifiés non commités, commits non poussés) ; sans branche distante suivie :
    GitError."""
    changes = await git(repo, "status", "--porcelain")
    return len(changes.splitlines()), await _count(repo, "@{u}..HEAD")


async def backup(repo: Path) -> str:
    """Commit de toutes les modifications (message daté) puis push ; renvoie un résumé."""
    changes = len((await git(repo, "status", "--porcelain")).splitlines())
    if changes:
        await git(repo, "add", "--all")
        message = f"sauvegarde du {datetime.now():%Y-%m-%d %H:%M}"
        await git(repo, *await _identity(repo), "commit", "--quiet", "-m", message)
    ahead = await _count(repo, "@{u}..HEAD")
    if ahead:
        try:
            await git(repo, "push", "--quiet")
        except GitError as err:
            if "[rejected]" not in err.output:
                raise
            done = f"{changes} fichier(s) commité(s), " if changes else ""
            raise GitError(f"{done}push refusé : le dépôt distant a changé, télécharger "
                           "d'abord puis sauvegarder à nouveau") from err
    if not changes and not ahead:
        return "rien à sauvegarder"
    return f"{changes} fichier(s) commité(s), {ahead} commit(s) poussé(s)"


async def _identity(repo: Path) -> tuple[str, ...]:
    try:
        await git(repo, "config", "user.email")
        return ()
    except GitError:
        return IDENTITY


async def head(repo: Path) -> str:
    return await git(repo, "rev-parse", "HEAD")


async def behind(repo: Path, fetch: bool = True) -> int:
    """Commits de la branche distante suivie pas encore tirés (après un fetch)."""
    if fetch:
        await git(repo, "fetch", "--quiet")
    return await _count(repo, "HEAD..@{u}")


async def pull(repo: Path, merge: bool = False) -> list[str]:
    """Tire la branche distante et renvoie les fichiers modifiés. Sans `merge` : avance rapide
    seulement (rien n'est fusionné ni écrasé). Avec : commits locaux et distants fusionnés ; en
    cas de conflit, la fusion est annulée (dépôt laissé tel quel)."""
    before = await head(repo)
    if not merge:
        await git(repo, "pull", "--ff-only", "--quiet")
    else:
        try:
            await git(repo, *await _identity(repo), "pull", "--no-rebase", "--no-edit", "--quiet")
        except GitError as err:
            if "CONFLICT" in err.output:
                await git(repo, "merge", "--abort")
                raise GitError("conflit entre les modifications locales et distantes, "
                               "téléchargement annulé (à résoudre à la main)", err.output) from err
            if "would be overwritten" in err.output:
                raise GitError("des fichiers modifiés localement changent aussi à distance : "
                               "sauvegarder d'abord", err.output) from err
            raise
    after = await head(repo)
    if before == after:
        return []
    return (await git(repo, "diff", "--name-only", before, after)).splitlines()
