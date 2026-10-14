"""Zigbee : coordinateur (clé USB EmberZNet via bellows/zigpy), commun à tous les pilotes Zigbee.

Le réseau (clés, canal) est mémorisé dans la clé elle-même. La base zigpy contient la liste des
équipements, leur interview (endpoints, clusters) et des sauvegardes du réseau qui permettent de
le reconstruire sur une nouvelle clé : c'est de la configuration. zigpy l'utilise dans temp/
(peut être en RAM) ; une copie cohérente est gardée dans pairing/zigbee/zigbee.db, écrite à
l'arrêt de la clé et une fois par jour, et reprise au démarrage si temp/ est vide.

Comme pour HomeKit, l'équipement logique (devices/<id>.py) ne contient aucune adresse : le lien
avec l'appareil physique est pairing/zigbee/<id>.toml (IEEE, fabricant, modèle, clusters),
écrit par l'appairage. Remplacer l'appareil ne change que ce fichier.

Une seule clé pour tous les équipements : le coordinateur est partagé (`coordinator`), démarré
au premier équipement Zigbee et relancé si la clé décroche. Le port série ne s'ouvre qu'une
fois : l'appairage (./habctrl pair zigbee) et HabController ne doivent pas tourner ensemble.
"""

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import tomllib
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from bellows.ash import AshException
from bellows.zigbee.application import ControllerApplication
from zigpy.config import (
    CONF_DATABASE,
    CONF_DEVICE,
    CONF_DEVICE_BAUDRATE,
    CONF_DEVICE_PATH,
)
from zigpy.application import ControllerApplication as ZigpyApplication
from zigpy.device import Device as ZigpyDevice
from zigpy.exceptions import RadioException, ZigbeeException
from zigpy.types import EUI64
from zigpy.zcl import AttributeReportedEvent, AttributeUpdatedEvent, ReportingConfig
from zigpy.zcl.foundation import Status

from core import PAIRING_DIR, TEMP_DIR, settings
from core.base import DeviceError, NetworkDevice

log = logging.getLogger(__name__)

ERRORS = (ZigbeeException, RadioException, AshException, OSError)  # protocole ou liaison série
DB_FILE = TEMP_DIR / "zigbee.db"  # base vivante, utilisée par zigpy
PAIRING = PAIRING_DIR / "zigbee"  # un fichier <id>.toml par équipement, et l'instantané
SNAPSHOT = PAIRING / "zigbee.db"  # copie de sauvegarde de la base, dans la configuration
SNAPSHOT_PERIOD = 24 * 3600  # s
RETRY_MIN = 2  # s, entre deux tentatives d'ouverture de la clé (doublé à chaque échec)
RETRY_MAX = 60


class PairingError(Exception):
    """Appairage absent ou illisible pour cet équipement."""


def pairing_file(dev_id: str) -> Path:
    if not re.fullmatch(r"[\w-]+", dev_id) or dev_id == "zigbee":
        raise ValueError(f"identifiant invalide « {dev_id} »")
    return PAIRING / f"{dev_id}.toml"


def _clusters(clusters) -> str:
    return json.dumps([f"0x{cid:04x}" for cid in sorted(clusters)])


def save_pairing(dev_id: str, zdev) -> Path:
    """Enregistre le lien entre l'équipement logique `dev_id` et l'appareil physique `zdev`."""
    lines = [
        f"# Appairage Zigbee de « {dev_id} » : lien entre l'équipement logique"
        f" (devices/{dev_id}.py)",
        "# et l'appareil physique. Écrit par ./habctrl pair zigbee.",
        "",
        f'ieee = "{zdev.ieee}"',
        f"manufacturer = {json.dumps(zdev.manufacturer or '', ensure_ascii=False)}",
        f"model = {json.dumps(zdev.model or '', ensure_ascii=False)}",
        f"paired = {datetime.now().replace(microsecond=0).isoformat()}",
    ]
    for ep_id, endpoint in sorted(zdev.endpoints.items()):
        if ep_id == 0:  # endpoint ZDO : présent partout, sans intérêt ici
            continue
        lines += [
            "",
            f"[endpoints.{ep_id}]",
            f"in = {_clusters(endpoint.in_clusters)}",
            f"out = {_clusters(endpoint.out_clusters)}",
        ]
    path = pairing_file(dev_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_pairing(dev_id: str) -> dict:
    """Appairage de l'équipement, avec son adresse convertie (clé « ieee » : EUI64)."""
    path = pairing_file(dev_id)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise PairingError(
            f"appairage absent ({path}) : lancer ./habctrl pair zigbee {dev_id}"
        ) from err
    except tomllib.TOMLDecodeError as err:
        raise PairingError(f"fichier d'appairage illisible ({path}) : {err}") from err
    try:
        data["ieee"] = EUI64.convert(data["ieee"])
    except (KeyError, AssertionError, ValueError) as err:
        raise PairingError(f"adresse IEEE absente ou invalide dans {path}") from err
    return data


def paired_ids(ieee: EUI64) -> list[str]:
    """Équipements logiques déjà rattachés à cet appareil physique."""
    found = []
    for path in sorted(PAIRING.glob("*.toml")):
        try:
            if load_pairing(path.stem)["ieee"] == ieee:
                found.append(path.stem)
        except (PairingError, ValueError):
            continue
    return found


def load_config() -> dict:
    """Configuration zigpy : port de la clé ([zigbee] de habctrl.toml) et base des équipements."""
    conf = settings("zigbee")
    if not conf.get("port"):
        raise ValueError("habctrl.toml : « port » manquant dans [zigbee]")
    return {
        CONF_DEVICE: {
            CONF_DEVICE_PATH: conf["port"],
            CONF_DEVICE_BAUDRATE: int(conf.get("baudrate", 115200)),
        },
        CONF_DATABASE: str(DB_FILE),
    }


@asynccontextmanager
async def open_coordinator():
    """Coordinateur démarré (réseau créé s'il n'existe pas encore), arrêté à la sortie."""
    config = load_config()
    restore_snapshot()
    # new() charge la base avant de démarrer : sans elle, les équipements connus sont oubliés
    app = await ControllerApplication.new(config, auto_form=True)
    try:
        yield app
    finally:
        await app.shutdown()
        save_snapshot()


def restore_snapshot() -> None:
    """Installation neuve ou temp/ vidé (RAM) : la base repart de la copie de pairing/."""
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not DB_FILE.exists() and SNAPSHOT.exists():
        shutil.copyfile(SNAPSHOT, DB_FILE)
        log.info("Base Zigbee reprise de %s", SNAPSHOT)


def save_snapshot() -> None:
    """Copie cohérente de la base vivante (API de sauvegarde SQLite : sûre même en écriture)."""
    if not DB_FILE.exists():
        return
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT.with_suffix(".tmp")
    try:
        source = sqlite3.connect(DB_FILE)
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        tmp.replace(SNAPSHOT)
    except (sqlite3.Error, OSError) as err:
        log.warning("Sauvegarde de la base Zigbee impossible (%s)", err)
        tmp.unlink(missing_ok=True)


def joined_devices(app: ZigpyApplication) -> list:
    """Équipements du réseau, sans le coordinateur."""
    coordinator_ieee = app.state.node_info.ieee
    return [device for ieee, device in app.devices.items() if ieee != coordinator_ieee]


def describe(device) -> list[str]:
    """Identité et clusters de chaque endpoint, pour choisir ou écrire un pilote."""
    lines = [
        f"IEEE : {device.ieee}   NWK : 0x{device.nwk:04x}",
        f"Fabricant : {device.manufacturer or '?'}   Modèle : {device.model or '?'}",
    ]
    for ep_id, endpoint in device.endpoints.items():
        if ep_id == 0:  # endpoint ZDO : présent partout, sans intérêt ici
            continue
        clusters_in = ", ".join(f"0x{cid:04x}" for cid in sorted(endpoint.in_clusters))
        clusters_out = ", ".join(f"0x{cid:04x}" for cid in sorted(endpoint.out_clusters))
        lines.append(f"Endpoint {ep_id} : entrée [{clusters_in}]  sortie [{clusters_out}]")
    return lines


def find_cluster(zdev, cluster_id: int):
    """Premier cluster serveur `cluster_id` de l'équipement, ou None."""
    for ep_id, endpoint in zdev.endpoints.items():
        if ep_id != 0 and cluster_id in endpoint.in_clusters:
            return endpoint.in_clusters[cluster_id]
    return None


async def read(cluster, names: list[str]) -> dict:
    """Lit des attributs ; ceux que l'équipement ne gère pas sont absents du résultat."""
    success, _failure = await cluster.read_attributes(names)
    return {name: value for name, value in success.items() if not isinstance(value, Status)}


async def setup_reporting(name: str, cluster, reports: dict[str, tuple[int, int, int]]) -> None:
    """Binding vers le coordinateur et envoi spontané des attributs {nom: (min s, max s, écart)}.

    Facultatif : si l'équipement refuse, l'état reste à jour par la lecture périodique.
    """
    try:
        await cluster.bind()
        await cluster.configure_reporting_multiple(
            {cluster.find_attribute(attr): ReportingConfig(*cfg) for attr, cfg in reports.items()}
        )
    except ERRORS as err:
        log.warning("%s : reporting non configuré sur 0x%04x (%s)", name, cluster.cluster_id, err)


class Coordinator:
    """La clé partagée : ouverte tant qu'il reste un équipement Zigbee, rouverte si elle décroche.

    Reçoit aussi les événements réseau de zigpy (méthodes au nom de l'événement).
    """

    def __init__(self):
        self.app: ZigpyApplication | None = None
        self._devices: dict[EUI64, "ZigbeeDevice"] = {}
        self._task: asyncio.Task | None = None
        self._lost: asyncio.Event | None = None

    def register(self, device: "ZigbeeDevice", ieee: EUI64) -> None:
        if (other := self._devices.get(ieee)) is not None and other is not device:
            log.error("%s et %s sont appairés au même appareil %s", other.id, device.id, ieee)
        self._devices[ieee] = device
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="zigbee")
        elif self.app is not None:
            device._attach(self.app)

    async def unregister(self, ieee: EUI64) -> None:
        self._devices.pop(ieee, None)
        if not self._devices and self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        backoff = RETRY_MIN
        while True:
            self._lost = asyncio.Event()
            try:
                async with open_coordinator() as app:
                    log.info("Clé Zigbee démarrée")
                    backoff = RETRY_MIN
                    self.app = app
                    app.add_listener(self)
                    for device in self._devices.values():
                        device._attach(app)
                    while not self._lost.is_set():
                        try:
                            await asyncio.wait_for(self._lost.wait(), SNAPSHOT_PERIOD)
                        except TimeoutError:
                            await asyncio.to_thread(save_snapshot)
                    log.warning("Clé Zigbee déconnectée, réouverture")
            except ValueError as err:  # habctrl.toml incorrect : inutile de réessayer
                log.error("Clé Zigbee : %s", err)
                return
            except ERRORS as err:
                log.warning(
                    "Clé Zigbee inaccessible (%s : %s), nouvel essai dans %s s",
                    type(err).__name__,
                    err,
                    backoff,
                )
            finally:
                self.app = None
                for device in self._devices.values():
                    device._detach()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RETRY_MAX)

    # --- événements de zigpy (appelés dans la boucle asyncio) ---------------

    def connection_lost(self, _exc: Exception) -> None:
        if self._lost is not None:
            self._lost.set()

    def device_initialized(self, zdev) -> None:
        """Équipement (ré)interviewé : zigpy a pu remplacer son objet, on s'y rattache."""
        device = self._devices.get(zdev.ieee)
        if device is not None and self.app is not None:
            device._attach(self.app)

    def device_left(self, zdev) -> None:
        device = self._devices.get(zdev.ieee)
        if device is not None:
            log.warning("%s : a quitté le réseau Zigbee", device.name)
            device._set_connected(False)


coordinator = Coordinator()


class ZigbeeDevice(NetworkDevice):
    """Base des pilotes Zigbee : rattachement au coordinateur, lecture périodique, joignabilité.

    Le pilote décrit son équipement par trois méthodes : `_configure` (binding et reporting,
    à chaque rattachement), `_read` (lecture complète de l'état, lève une erreur de ERRORS si
    l'équipement ne répond pas) et `_on_attribute` (valeur reçue spontanément).
    L'équipement est joignable tant qu'il répond : une lecture toutes les `poll` secondes le
    vérifie (et rattrape les valeurs que l'équipement n'envoie pas de lui-même).
    """

    POLL = 300  # s, période de lecture par défaut

    def __init__(self, name: str = "", poll: float | None = None):
        super().__init__(name)
        self.ieee: EUI64 | None = None  # lue dans pairing/zigbee/<id>.toml au démarrage
        self.poll = self.POLL if poll is None else poll
        self.zdev = None  # objet zigpy de l'équipement, tant que la clé est ouverte
        self._unsubscribe: list = []
        self._tasks: set[asyncio.Task] = set()

    # --- cycle de vie -------------------------------------------------------

    async def start(self) -> None:
        try:
            ieee: EUI64 = load_pairing(self.id)["ieee"]
        except PairingError as err:
            log.error("%s : %s", self.name, err)
            return  # reste injoignable
        self.ieee = ieee
        coordinator.register(self, ieee)

    async def stop(self) -> None:
        if self.ieee is not None:
            await coordinator.unregister(self.ieee)
        self._detach()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (err := task.exception()):
            log.error("%s : erreur inattendue", self.name, exc_info=err)

    def _attach(self, app: ZigpyApplication) -> None:
        """Clé ouverte ou équipement réinterviewé : écoute et lecture de l'objet zigpy courant."""
        self._detach()
        if self.ieee is None:
            return
        zdev = app.devices.get(self.ieee)
        if not isinstance(zdev, ZigpyDevice):
            log.error(
                "%s : %s inconnu du réseau Zigbee (./habctrl pair zigbee)", self.name, self.ieee
            )
            return
        self.zdev = zdev
        for endpoint_id, endpoint in zdev.endpoints.items():
            if endpoint_id == 0:
                continue
            for cluster in endpoint.in_clusters.values():
                # rapport spontané de l'équipement (attribute_report), ou valeur corrigée par
                # un quirk (attribute_updated) : zigpy n'émet qu'un des deux par valeur reçue
                for event in (AttributeReportedEvent, AttributeUpdatedEvent):
                    self._unsubscribe.append(cluster.on_event(event.event_type, self._on_event))
        self._spawn(self._poll_loop(zdev))

    def _detach(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        for task in self._tasks:
            task.cancel()
        self.zdev = None
        self._set_connected(False)

    async def _poll_loop(self, zdev) -> None:
        configured = False
        while True:
            try:
                if not configured:
                    await self._configure(zdev)
                    configured = True
                await self._read(zdev)
            except ERRORS as err:
                if self._connected:
                    log.warning("%s : ne répond plus (%s)", self.name, type(err).__name__)
                self._set_connected(False)
            else:
                if not self._connected:
                    log.info("%s : connecté", self.name)
                self._synced = True
                self._set_connected(True)
            await asyncio.sleep(self.poll)

    def _set_connected(self, connected: bool) -> None:
        self._connected = connected
        if not connected:
            self._synced = False
        self._notify()

    # --- à fournir par le pilote --------------------------------------------

    async def _configure(self, zdev) -> None:
        """Binding et reporting (voir setup_reporting)."""

    async def _read(self, zdev) -> None:
        """Lit tout l'état de l'équipement."""
        raise NotImplementedError

    def _on_attribute(self, cluster_id: int, name: str, value) -> None:
        """Valeur reçue de l'équipement (envoi spontané ou réponse à une lecture)."""

    # --- événements de zigpy ------------------------------------------------

    def _on_event(self, event: AttributeReportedEvent | AttributeUpdatedEvent) -> None:
        if event.attribute_name is None:  # attribut propre au fabricant, inconnu de zigpy
            return
        self._on_attribute(event.cluster_id, event.attribute_name, event.value)
        self._notify()

    # --- commandes ----------------------------------------------------------

    async def _command(self, cluster, command: str, *args) -> None:
        """Envoie une commande ZCL ; DeviceError si l'équipement ne répond pas ou la refuse."""
        if cluster is None or not (self._connected and self._synced):
            raise DeviceError(f"{self.name} : injoignable")
        try:
            result = await getattr(cluster, command)(*args)
        except ERRORS as err:
            self._set_connected(False)
            raise DeviceError(f"{self.name} : {type(err).__name__}") from err
        status = getattr(result, "status", Status.SUCCESS)
        if status != Status.SUCCESS:
            raise DeviceError(f"{self.name} : commande {command} refusée ({status.name})")
