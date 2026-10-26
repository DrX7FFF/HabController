import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import git  # noqa: E402
from core.notifications import Notifications  # noqa: E402
from core.watch import BACKUP, CONFIG, UPDATE, Watch, modified_since  # noqa: E402


def run(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    run(repo, "git", "add", "--all")
    run(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)


class RepoTest(unittest.IsolatedAsyncioTestCase):
    """Un dépôt distant (nu) et deux clones : `repo` (surveillé) et `other` (autre machine)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.remote, self.repo, self.other = root / "remote.git", root / "repo", root / "other"
        run(root, "git", "init", "-q", "--bare", "-b", "main", str(self.remote))
        run(root, "git", "clone", "-q", str(self.remote), str(self.repo))
        commit(self.repo, "habctrl.toml", "[paths]\n")
        run(self.repo, "git", "push", "-q", "-u", "origin", "HEAD:main")
        run(root, "git", "clone", "-q", str(self.remote), str(self.other))
        self.notifications = Notifications()
        self.watch = Watch(self.notifications, config_dir=self.repo, code_dir=self.repo)

    def tearDown(self):
        self.tmp.cleanup()


class ConfigWatchTest(RepoTest):
    def test_ignored_dirs(self):
        since = time.time()
        os.utime(self.repo, (since - 10, since - 10))
        for name in ("pairing", ".git"):
            (self.repo / name).mkdir(exist_ok=True)
            os.utime(self.repo, (since - 10, since - 10))  # dossier créé avant le chargement
            (self.repo / name / "x").write_text("", encoding="utf-8")
        self.assertEqual(modified_since(since, self.repo), [])

    def test_notification_until_reload(self):
        os.utime(self.repo / "habctrl.toml", (time.time() - 10,) * 2)
        os.utime(self.repo, (time.time() - 10,) * 2)
        self.watch.mark_loaded()
        self.assertIsNone(self.notifications.get(CONFIG))
        (self.repo / "habctrl.toml").write_text("[zigbee]\n", encoding="utf-8")
        self.watch.check_config()
        self.assertIn("habctrl.toml", self.notifications.get(CONFIG)["text"])
        self.watch.mark_loaded()
        self.assertIsNone(self.notifications.get(CONFIG))

    def test_deleted_file(self):
        old = time.time() - 10
        os.utime(self.repo / "habctrl.toml", (old, old))
        os.utime(self.repo, (old, old))
        self.watch.loaded_at = old + 5
        self.watch.check_config()
        self.assertIsNone(self.notifications.get(CONFIG))
        (self.repo / "habctrl.toml").unlink()
        self.watch.check_config()
        self.assertIn("modifiée", self.notifications.get(CONFIG)["text"])


class BackupTest(RepoTest):
    async def test_backup_commits_and_pushes(self):
        await self.watch.check_backup()
        self.assertIsNone(self.notifications.get(BACKUP))
        (self.repo / "habctrl.toml").write_text("[zigbee]\n", encoding="utf-8")
        await self.watch.check_backup()
        self.assertIn("1 fichier(s) modifié(s)", self.notifications.get(BACKUP)["text"])
        self.assertEqual(await git.backup(self.repo), "1 fichier(s) commité(s), 1 commit(s) poussé(s)")
        await self.watch.check_backup()
        self.assertIsNone(self.notifications.get(BACKUP))
        self.assertEqual(await git.backup(self.repo), "rien à sauvegarder")
        run(self.other, "git", "pull", "-q")
        self.assertEqual((self.other / "habctrl.toml").read_text(encoding="utf-8"), "[zigbee]\n")

    async def test_rejected_push(self):
        commit(self.other, "autre.toml", "")
        run(self.other, "git", "push", "-q")
        commit(self.repo, "local.toml", "")
        with self.assertRaises(git.GitError):
            await git.backup(self.repo)

    async def test_unreadable_state(self):
        run(self.repo, "git", "branch", "-q", "--unset-upstream")
        with self.assertLogs("core.watch", "WARNING"):
            await self.watch.check_backup()
        self.assertEqual(self.notifications.get(BACKUP)["level"], "error")


class UpdateTest(RepoTest):
    async def test_update_then_restart_pending(self):
        await self.watch.check_update()
        self.assertIsNone(self.notifications.get(UPDATE))
        commit(self.other, "requirements.txt", "fastapi\n")
        run(self.other, "git", "push", "-q")
        await self.watch.check_update()
        self.assertIn("1 commit(s)", self.notifications.get(UPDATE)["text"])
        self.assertEqual(await git.pull(self.repo), ["requirements.txt"])
        await self.watch.check_update(fetch=False)
        self.assertIn("redémarrage à faire", self.notifications.get(UPDATE)["text"])
        self.assertEqual(await git.pull(self.repo), [])

    async def test_diverged_pull_refused(self):
        commit(self.other, "a.py", "")
        run(self.other, "git", "push", "-q")
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
