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
    pass


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
        message = (err or out).decode(errors="replace").strip().splitlines()
        raise GitError(f"git {name} : {message[-1] if message else proc.returncode}")
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
        try:
            await git(repo, "config", "user.email")
            identity = ()
        except GitError:
            identity = IDENTITY
        message = f"sauvegarde du {datetime.now():%Y-%m-%d %H:%M}"
        await git(repo, *identity, "commit", "--quiet", "-m", message)
    ahead = await _count(repo, "@{u}..HEAD")
    if ahead:
        await git(repo, "push", "--quiet")
    if not changes and not ahead:
        return "rien à sauvegarder"
    return f"{changes} fichier(s) commité(s), {ahead} commit(s) poussé(s)"


async def head(repo: Path) -> str:
    return await git(repo, "rev-parse", "HEAD")


async def behind(repo: Path, fetch: bool = True) -> int:
    """Commits de la branche distante suivie pas encore tirés (après un fetch)."""
    if fetch:
        await git(repo, "fetch", "--quiet")
    return await _count(repo, "HEAD..@{u}")


async def pull(repo: Path) -> list[str]:
    """Tire la branche distante (avance rapide seulement : rien n'est fusionné ni écrasé) ;
    renvoie les fichiers modifiés."""
    before = await head(repo)
    await git(repo, "pull", "--ff-only", "--quiet")
    after = await head(repo)
    if before == after:
        return []
    return (await git(repo, "diff", "--name-only", before, after)).splitlines()
