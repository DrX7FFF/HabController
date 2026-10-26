import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import git, sync  # noqa: E402
from core.notifications import Notifications  # noqa: E402


def run(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    run(repo, "git", "add", "--all")
    run(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)


class RepoTest(unittest.IsolatedAsyncioTestCase):
    """Un dépôt distant (nu) et deux clones : `repo` (cette machine) et `other` (une autre)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote, self.repo, self.other = root / "remote.git", root / "repo", root / "other"
        run(root, "git", "init", "-q", "--bare", "-b", "main", str(self.remote))
        run(root, "git", "clone", "-q", str(self.remote), str(self.repo))
        commit(self.repo, "habctrl.toml", "[paths]\n")
        run(self.repo, "git", "push", "-q", "-u", "origin", "HEAD:main")
        run(root, "git", "clone", "-q", str(self.remote), str(self.other))

    def tearDown(self):
        self.tmp.cleanup()

    def push_other(self, name: str) -> None:
        commit(self.other, name, "")
        run(self.other, "git", "push", "-q")


class LoadTest(RepoTest):
    def age(self, *paths: Path) -> float:
        """Dates des chemins reculées de 10 s ; renvoie une date de chargement postérieure."""
        old = time.time() - 10
        for path in paths:
            os.utime(path, (old, old))
        return old + 5

    def test_ignored_dirs(self):
        for name in ("pairing", ".git"):
            (self.repo / name).mkdir(exist_ok=True)
        loaded_at = self.age(self.repo, self.repo / "habctrl.toml")
        for name in ("pairing", ".git"):
            (self.repo / name / "x").write_text("", encoding="utf-8")
        self.assertEqual(sync.modified_since(loaded_at, self.repo), [])
        self.assertFalse(sync.check_load(self.repo, loaded_at)["pending"])

    def test_modified_or_deleted(self):
        loaded_at = self.age(self.repo, self.repo / "habctrl.toml")
        (self.repo / "habctrl.toml").write_text("[zigbee]\n", encoding="utf-8")
        check = sync.check_load(self.repo, loaded_at)
        self.assertTrue(check["pending"])
        self.assertIn("habctrl.toml", check["text"])
        (self.repo / "habctrl.toml").unlink()
        self.assertEqual(sync.modified_since(loaded_at, self.repo), ["repo/"])


class SaveTest(RepoTest):
    async def test_save_commits_and_pushes(self):
        self.assertFalse((await sync.check_save(self.repo))["pending"])
        (self.repo / "habctrl.toml").write_text("[zigbee]\n", encoding="utf-8")
        check = await sync.check_save(self.repo)
        self.assertEqual(check, {"pending": True, "text": "1 fichier(s) modifié(s)."})
        self.assertEqual(await git.backup(self.repo),
                         "1 fichier(s) commité(s), 1 commit(s) poussé(s)")
        self.assertFalse((await sync.check_save(self.repo))["pending"])
        self.assertEqual(await git.backup(self.repo), "rien à sauvegarder")
        run(self.other, "git", "pull", "-q")
        self.assertEqual((self.other / "habctrl.toml").read_text(encoding="utf-8"), "[zigbee]\n")

    async def test_rejected_push_then_download_and_save(self):
        self.push_other("autre.toml")
        (self.repo / "local.toml").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(git.GitError, "1 fichier.*push refusé.*télécharger"):
            await git.backup(self.repo)
        self.assertEqual(await git.pull(self.repo, merge=True), ["autre.toml"])
        self.assertEqual(await git.backup(self.repo), "0 fichier(s) commité(s), 2 commit(s) poussé(s)")
        run(self.other, "git", "pull", "-q")
        self.assertTrue((self.other / "local.toml").exists())

    async def test_download_conflict_aborted(self):
        commit(self.other, "habctrl.toml", "[distant]\n")
        run(self.other, "git", "push", "-q")
        commit(self.repo, "habctrl.toml", "[local]\n")
        with self.assertRaisesRegex(git.GitError, "conflit"):
            await git.pull(self.repo, merge=True)
        self.assertEqual((self.repo / "habctrl.toml").read_text(encoding="utf-8"), "[local]\n")
        self.assertEqual(await git.git(self.repo, "status", "--porcelain"), "")

    async def test_download_overwrite_refused(self):
        commit(self.other, "habctrl.toml", "[distant]\n")
        run(self.other, "git", "push", "-q")
        (self.repo / "habctrl.toml").write_text("[local]\n", encoding="utf-8")
        with self.assertRaisesRegex(git.GitError, "sauvegarder d'abord"):
            await git.pull(self.repo, merge=True)

    async def test_no_upstream(self):
        run(self.repo, "git", "branch", "-q", "--unset-upstream")
        with self.assertRaises(git.GitError):
            await sync.check_save(self.repo)


class PullTest(RepoTest):
    async def test_download(self):
        self.assertFalse((await sync.check_download(self.repo))["pending"])
        self.push_other("devices.py")
        self.assertEqual(await sync.check_download(self.repo),
                         {"pending": True, "text": "1 commit(s) à télécharger."})
        self.assertEqual(await git.pull(self.repo), ["devices.py"])
        self.assertFalse((await sync.check_download(self.repo))["pending"])

    async def test_app_then_restart_pending(self):
        boot = await git.head(self.repo)
        self.assertFalse((await sync.check_app(self.repo, boot))["pending"])
        self.push_other("requirements.txt")
        self.assertIn("1 commit(s)", (await sync.check_app(self.repo, boot))["text"])
        self.assertEqual(await git.pull(self.repo), ["requirements.txt"])
        self.assertIn("redémarrage", (await sync.check_app(self.repo, boot))["text"])
        self.assertEqual(await git.pull(self.repo), [])

    async def test_diverged_pull_refused(self):
        self.push_other("a.py")
        commit(self.repo, "b.py", "")
        await git.git(self.repo, "fetch")
        with self.assertRaises(git.GitError):
            await git.pull(self.repo)


class NotificationsTest(unittest.TestCase):
    def test_set_keeps_date_clear_removes(self):
        n = Notifications()
        n.set("a", "un")
        since = n.get("a")["since"]
        n.set("a", "deux", "error")
        self.assertEqual(n.list(), [{"id": "a", "text": "deux", "level": "error", "since": since}])
        n.clear("a")
        n.clear("absent")
        self.assertEqual(n.list(), [])


if __name__ == "__main__":
    unittest.main()
