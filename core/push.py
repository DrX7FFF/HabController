"""Notifications push Android par ntfy.sh (serveur public, sans compte). Deux topics aléatoires,
créés au premier démarrage dans HabSecrets/ntfy.json : le nom d'un topic sert de mot de passe.
- notify : les notifications, à saisir dans l'appli ntfy (affiché dans l'Administration) ;
- reply : les boutons d'une question y publient « <question>:<n° du bouton> » ; HabController y
  est abonné et appelle l'action du bouton. Une question n'a qu'une réponse."""

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Callable

import aiohttp

from core import CONFIG_DIR
from core.base import Retry, describe, run_action, spawn_task

log = logging.getLogger(__name__)
__all__ = ["Question", "notify"]

SERVER = "https://ntfy.sh"
SECRETS_FILE = CONFIG_DIR.parent / "HabSecrets" / "ntfy.json"
TIMEOUT = aiohttp.ClientTimeout(total=10)
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_read=120)  # keepalive du serveur : 45 s


def load_topics() -> dict[str, str]:
    """Topics de HabSecrets/ntfy.json, tirés au sort s'il n'existe pas encore."""
    try:
        return json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        topics = {"notify": secrets.token_urlsafe(24), "reply": secrets.token_urlsafe(24)}
        SECRETS_FILE.parent.mkdir(mode=0o700, exist_ok=True)
        SECRETS_FILE.touch(mode=0o600)
        SECRETS_FILE.write_text(json.dumps(topics, indent=2), encoding="utf-8")
        log.info("push : topics créés dans %s", SECRETS_FILE)
        return topics


class Question:
    """Notification à boutons (trois au plus), déclarée une fois comme un Timer : ask() l'envoie
    (en retirant la précédente), cancel() la retire du téléphone, sans effet si elle n'est pas
    posée. L'action du bouton choisi est appelée (fonction simple ou async)."""

    def __init__(self, text: str, buttons: dict[str, Callable | None]):
        if len(buttons) > 3:
            raise ValueError("push : trois boutons au plus")
        self.text = text
        self.buttons = buttons
        self.id: str | None = None  # sequence_id ntfy de la notification posée

    def ask(self) -> None:
        if _push:
            _push.ask(self)

    def cancel(self) -> None:
        if _push:
            _push.withdraw(self)


class Push:
    def __init__(self):
        topics = load_topics()
        self.topic, self.reply = topics["notify"], topics["reply"]
        self.questions: dict[str, Question] = {}
        self._session: aiohttp.ClientSession | None = None
        self._listen_task: asyncio.Task | None = None
        self._sends: set[asyncio.Task] = set()  # hors de base.tasks : part même au rechargement

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=TIMEOUT)
        self._listen_task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
            await asyncio.gather(self._listen_task, return_exceptions=True)
        await asyncio.gather(*self._sends, return_exceptions=True)
        if self._session:
            await self._session.close()

    def publish(self, method: str, url: str, data: dict | None = None) -> None:
        task = asyncio.create_task(self._request(method, url, data))
        self._sends.add(task)
        task.add_done_callback(self._sends.discard)

    async def _request(self, method: str, url: str, data: dict | None) -> None:
        assert self._session is not None, "Push.start() non appelé"
        try:
            async with self._session.request(method, url, json=data) as response:
                response.raise_for_status()
        except (aiohttp.ClientError, TimeoutError) as err:
            log.warning("push : envoi impossible (%s)", describe(err))

    def delete(self, sequence_id: str) -> None:
        """Retire la notification du téléphone."""
        self.publish("DELETE", f"{SERVER}/{self.topic}/{sequence_id}")

    def notify(self, text: str, data: dict | None = None) -> None:
        self.publish("POST", SERVER, {"topic": self.topic, "message": text, **(data or {})})

    def ask(self, question: Question) -> None:
        self.withdraw(question)
        question.id = secrets.token_hex(8)  # sequence_id ntfy : permet de supprimer la notification
        self.questions[question.id] = question
        actions = [{"action": "http", "label": label, "url": f"{SERVER}/{self.reply}",
                    "body": f"{question.id}:{n}", "clear": True}
                   for n, label in enumerate(question.buttons)]
        self.notify(question.text, {"sequence_id": question.id, "actions": actions})

    def withdraw(self, question: Question) -> None:
        """Retire la question du téléphone si elle est posée."""
        if question.id and self.questions.pop(question.id, None):
            self.delete(question.id)
        question.id = None

    def answer(self, body: str) -> None:
        qid, _, n = body.partition(":")
        question = self.questions.pop(qid, None)
        actions = list(question.buttons.values()) if question else []
        if question is None or not n.isdigit() or int(n) >= len(actions):
            log.info("push : réponse ignorée (question retirée ou inconnue)")
            return
        question.id = None
        self.delete(qid)  # traitée : retirée de tous les appareils abonnés
        if action := actions[int(n)]:
            spawn_task(run_action(action, "push.réponse"), "push.réponse")

    async def _listen(self) -> None:
        """Abonnement au topic des réponses ; après une coupure, reprend au dernier message lu."""
        assert self._session is not None
        since, failing, retry = str(int(time.time())), False, Retry()
        while True:
            try:
                async with self._session.get(f"{SERVER}/{self.reply}/json", params={"since": since},
                                             timeout=STREAM_TIMEOUT) as response:
                    response.raise_for_status()
                    retry.reset()
                    if failing:
                        log.info("push : abonnement rétabli")
                        failing = False
                    async for line in response.content:
                        message = json.loads(line)
                        if message.get("event") == "message":
                            since = message["id"]
                            self.answer(message.get("message", ""))
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                if not failing:
                    log.warning("push : abonnement coupé (%s)", describe(err))
                failing = True
            await retry.wait()

    def forget(self) -> None:
        """Rechargement des automatisations : leurs questions en attente sont retirées."""
        for question in list(self.questions.values()):
            question.cancel()


_push: Push | None = None


def setup() -> Push:
    global _push
    _push = Push()
    return _push


def notify(text: str) -> None:
    """Notification simple sur le téléphone. Ne bloque pas."""
    if _push:
        _push.notify(text)


def forget() -> None:
    if _push:
        _push.forget()
