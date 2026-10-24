"""Zigbee : coordinateur (clé USB EmberZNet via bellows/zigpy), commun à tous les pilotes Zigbee.

Le réseau (clés, canal) est mémorisé dans la clé elle-même. La base zigpy contient la liste des
équipements, leur interview (endpoints, clusters) et des sauvegardes du réseau qui permettent de
le reconstruire sur une nouvelle clé : c'est de la configuration. zigpy l'utilise dans temp/
(peut être en RAM) ; une copie cohérente est gardée dans pairing/zigbee.db, écrite après
chaque appairage et reprise au démarrage si temp/ est vide. Changer de clé avec une copie
ancienne peut obliger à réappairer les équipements : le compteur de trames du réseau,
sauvegardé avec, a avancé depuis.

L'équipement logique (déclaré dans devices/) ne contient aucune adresse : le lien avec l'appareil
physique est une ligne de pairing/zigbee.toml (IEEE -> id), écrite par l'appairage. Ce fichier
fait foi : à chaque ouverture de la clé, les appareils de la base qui n'y sont pas sont retirés
du réseau (appareil remplacé, appairage perdu ou interrompu : il suffit de le réappairer), et
les incohérences sont signalées. Fichier absent ou illisible : rien n'est retiré (protège d'un
mauvais chemin de configuration). Repartir de zéro : supprimer pairing/zigbee.toml,
pairing/zigbee.db et temp/zigbee.db.

Une seule clé pour tous les équipements : le coordinateur est partagé (`coordinator`), démarré
au premier équipement Zigbee et relancé si la clé décroche. L'appairage l'emprunte
(`coordinator.session()`) : le port série ne s'ouvre qu'une fois.
"""

import asyncio
import json
import logging
import shutil
import sqlite3
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path

from bellows.ash import AshException
import zhaquirks
from bellows.zigbee.application import ControllerApplication
from zha.quirks import DEVICE_REGISTRY
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
MAPPING = PAIRING_DIR / "zigbee.toml"  # IEEE -> équipement logique, fait foi
SNAPSHOT = PAIRING_DIR / "zigbee.db"  # copie de sauvegarde de la base, dans la configuration
MODELS = Path(__file__).with_name("models.toml")  # modèle ou type d'appareil -> pilote
HOME_AUTOMATION = 0x0104  # profil Zigbee des types d'appareil standard
RETRY_MIN = 2  # s, entre deux tentatives d'ouverture de la clé (doublé à chaque échec)
RETRY_MAX = 60
READY_WAIT = 30  # s, laissées à la clé pour s'ouvrir quand l'appairage l'emprunte


class PairingError(Exception):
    """Appairage absent ou illisible pour cet équipement."""


def load_mapping(missing_ok: bool = False) -> dict[EUI64, str]:
    """Correspondance IEEE -> id de pairing/zigbee.toml ; PairingError s'il est inutilisable."""
    try:
        data = tomllib.loads(MAPPING.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        if missing_ok:
            return {}
        raise PairingError(f"{MAPPING} introuvable") from err
    except tomllib.TOMLDecodeError as err:
        raise PairingError(f"{MAPPING} illisible : {err}") from err
    try:
        if not all(isinstance(dev_id, str) for dev_id in data.values()):
            raise ValueError("identifiant attendu entre guillemets")
        return {EUI64.convert(ieee): dev_id for ieee, dev_id in data.items()}
    except (AssertionError, ValueError) as err:
        raise PairingError(f"{MAPPING} : ligne invalide ({err})") from err


def save_mapping(mapping: dict[EUI64, str], devices: dict) -> Path:
    """Écrit la correspondance, avec en commentaire le modèle des appareils connus de la base."""
    lines = [
        "# Appareil Zigbee (IEEE) -> équipement logique (id dans devices/). Écrit par l'appairage.",
        "# Fait foi : à l'ouverture de la clé, les appareils absents d'ici sont retirés du réseau.",
        "",
    ]
    for ieee, dev_id in sorted(mapping.items(), key=lambda item: item[1]):
        line = f'"{ieee}" = {json.dumps(dev_id)}'
        if (zdev := devices.get(ieee)) is not None:
            line += f"  # {zdev.manufacturer or '?'} {zdev.model or '?'}"
        lines.append(line)
    MAPPING.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAPPING.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(MAPPING)
    return MAPPING


def links() -> dict[str, str]:
    """Appareils liés : {id: description} ({} si pairing/zigbee.toml est inutilisable)."""
    try:
        return {dev_id: f"Zigbee {ieee}" for ieee, dev_id in load_mapping(missing_ok=True).items()}
    except PairingError:
        return {}



def link_models() -> dict[str, tuple[list[str], str]]:
    """Appareils liés : {id: (clés de models.toml, commentaire de déclaration)}, lus dans la base
    zigpy (la vivante, sinon la copie) : la clé n'a pas besoin d'être ouverte."""
    db = DB_FILE if DB_FILE.exists() else SNAPSHOT
    try:
        mapping = load_mapping(missing_ok=True)
        with closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as con:
            tables = con.execute("SELECT name FROM sqlite_master WHERE name LIKE 'endpoints_v%'")
            version = max(int(name.removeprefix("endpoints_v")) for (name,) in tables)
            basic = con.execute(  # cluster Basic : 4 fabricant, 5 modèle
                f"SELECT ieee, attr_id, value FROM attributes_cache_v{version}"
                " WHERE cluster_type = 0 AND cluster_id = 0 AND attr_id IN (4, 5)"
            ).fetchall()
            types = con.execute(
                f"SELECT ieee, device_type FROM endpoints_v{version}"
                " WHERE endpoint_id != 0 AND profile_id = ? ORDER BY endpoint_id",
                (HOME_AUTOMATION,),
            ).fetchall()
    except (PairingError, sqlite3.Error, ValueError):
        return {}
    found = {}
    for ieee, dev_id in mapping.items():
        info = {attr: value for i, attr, value in basic if i == str(ieee)}
        model, manufacturer = info.get(5) or "", info.get(4) or "?"
        keys = [model, *(f"0x{dtype:04x}" for i, dtype in types if i == str(ieee))]
        found[dev_id] = (keys, f"Zigbee : {manufacturer}, modèle {model or '?'}")
    return found


def linked_ieee(dev_id: str) -> EUI64:
    """IEEE de l'appareil lié à l'équipement logique ; PairingError s'il n'y en a pas."""
    for ieee, linked_id in load_mapping().items():
        if linked_id == dev_id:
            return ieee
    raise PairingError(f"absent de {MAPPING} : l'appairer")


def device_types(zdev) -> list[str]:
    """Types d'appareil Zigbee Home Automation annoncés par les endpoints (ex. "0x010a")."""
    return [
        f"0x{endpoint.device_type:04x}"
        for ep_id, endpoint in zdev.endpoints.items()
        if ep_id != 0 and endpoint.profile_id == HOME_AUTOMATION
    ]


def model_keys(zdev) -> list[str]:
    """Clés cherchées dans models.toml : le modèle, puis les types d'appareil."""
    return [zdev.model or "", *device_types(zdev)]


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


_quirks_loaded = False


async def load_quirks():
    """Correctifs zha-quirks des appareils non standard, chargés une fois hors de la boucle
    (environ 2 s) ; renvoie de quoi les appliquer à chaque appareil de la base ou appairé."""
    global _quirks_loaded
    if not _quirks_loaded:
        await asyncio.to_thread(zhaquirks.setup)
        _quirks_loaded = True
    return DEVICE_REGISTRY.resolve


@asynccontextmanager
async def open_session():
    """Coordinateur démarré (réseau créé s'il n'existe pas encore), arrêté à la sortie."""
    config = load_config()
    restore_snapshot()
    # new() charge la base avant de démarrer : sans elle, les équipements connus sont oubliés
    app = await ControllerApplication.new(
        config, auto_form=True, device_resolver=await load_quirks()
    )
    try:
        await synchronize(app)
        yield app
    finally:
        await app.shutdown()


async def synchronize(app: ZigpyApplication) -> None:
    """Aligne la base sur pairing/zigbee.toml, qui fait foi, et signale les incohérences."""
    try:
        mapping = load_mapping()
    except PairingError as err:
        log.warning("Zigbee : %s, appareils non synchronisés", err)
        return
    for zdev in joined_devices(app):
        if zdev.ieee not in mapping:
            log.warning(
                "Zigbee : %s (%s %s) absent de %s, retiré du réseau",
                zdev.ieee,
                zdev.manufacturer,
                zdev.model,
                MAPPING.name,
            )
            try:
                await app.remove(zdev.ieee)
            except ERRORS as err:
                log.warning("Zigbee : retrait de %s impossible (%s)", zdev.ieee, err)
    for ieee, dev_id in mapping.items():
        if ieee not in app.devices:
            log.warning(
                "%s : déclaré avec l'IEEE %s mais absent du réseau Zigbee : le retirer de %s "
                "ou réappairer un appareil",
                dev_id,
                ieee,
                MAPPING.name,
            )


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
        lines.append(
            f"Endpoint {ep_id} (profil 0x{endpoint.profile_id:04x}, type"
            f" 0x{endpoint.device_type:04x}) : entrée [{clusters_in}]  sortie [{clusters_out}]"
        )
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
    """La clé partagée : ouverte tant qu'il reste un équipement Zigbee ou un appairage en cours,
    rouverte si elle décroche.

    Reçoit aussi les événements réseau de zigpy (méthodes au nom de l'événement).
    """

    def __init__(self):
        self.app: ZigpyApplication | None = None
        self._devices: dict[EUI64, "ZigbeeDevice"] = {}
        self._sessions = 0  # appairages qui empruntent la clé
        self._task: asyncio.Task | None = None
        self._lost: asyncio.Event | None = None
        self._ready = asyncio.Event()  # clé ouverte

    def register(self, device: "ZigbeeDevice", ieee: EUI64) -> None:
        self._devices[ieee] = device
        if self._task is None:
            self._launch()
        elif self.app is not None:
            device._attach(self.app)

    async def unregister(self, ieee: EUI64) -> None:
        self._devices.pop(ieee, None)
        await self._release()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ZigpyApplication]:
        """La clé ouverte, pour l'appairage (ouverte pour l'occasion si besoin)."""
        self._sessions += 1
        try:
            if self._task is None or self._task.done():
                self._launch()
            ready = asyncio.ensure_future(self._ready.wait())
            await asyncio.wait(
                {ready, self._task}, timeout=READY_WAIT, return_when=asyncio.FIRST_COMPLETED
            )
            ready.cancel()
            if self.app is None:
                raise RadioException("clé Zigbee inaccessible, voir le journal")
            yield self.app
        finally:
            self._sessions -= 1
            await self._release()

    def _launch(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name="zigbee")

    async def _release(self) -> None:
        """Plus personne n'utilise la clé : on la ferme."""
        if not self._devices and not self._sessions and self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        backoff = RETRY_MIN
        while True:
            self._lost = asyncio.Event()
            try:
                async with open_session() as app:
                    log.info("Clé Zigbee démarrée")
                    backoff = RETRY_MIN
                    self.app = app
                    self._ready.set()
                    app.add_listener(self)
                    for device in self._devices.values():
                        device._attach(app)
                    await self._lost.wait()
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
                self._ready.clear()
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
    Équipement sur pile (POLL = None) : il dort, on ne l'interroge jamais. Il est joignable dès
    son premier message, et configuré à ce moment-là (il est éveillé) ; pas de `_read`.
    """

    POLL: float | None = 300  # s, période de lecture par défaut ; None : sur pile

    def __init__(self, name: str = "", poll: float | None = None):
        super().__init__(name)
        self.ieee: EUI64 | None = None  # lue dans pairing/zigbee.toml au démarrage
        self.poll = self.POLL if poll is None else poll
        self.zdev = None  # objet zigpy de l'équipement, tant que la clé est ouverte
        self._unsubscribe: list = []
        self._tasks: set[asyncio.Task] = set()

    # --- cycle de vie -------------------------------------------------------

    async def start(self) -> None:
        try:
            ieee = linked_ieee(self.id)
        except PairingError:
            return  # reste injoignable, signalé par equipements.txt
        self.ieee = ieee
        coordinator.register(self, ieee)

    def physical(self) -> str:
        return links().get(self.id, "")

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
                "%s : %s inconnu du réseau Zigbee (l'appairer)", self.name, self.ieee
            )
            return
        self.zdev = zdev
        for endpoint_id, endpoint in zdev.endpoints.items():
            if endpoint_id == 0:
                continue
            # clusters de sortie aussi : certains capteurs y rapportent (ex. porte Xiaomi, 0x0006)
            for cluster in (*endpoint.in_clusters.values(), *endpoint.out_clusters.values()):
                # rapport spontané de l'équipement (attribute_report), ou valeur corrigée par
                # un quirk (attribute_updated) : zigpy n'émet qu'un des deux par valeur reçue
                for event in (AttributeReportedEvent, AttributeUpdatedEvent):
                    self._unsubscribe.append(cluster.on_event(event.event_type, self._on_event))
        if self.poll:
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
        self._received()
        self._on_attribute(event.cluster_id, event.attribute_name, event.value)
        self._notify()

    def _received(self) -> None:
        """Message reçu : un équipement sur pile devient joignable (et se configure)."""
        if not self.poll and not self._connected and self.zdev is not None:
            log.info("%s : connecté", self.name)
            self._connected = self._synced = True
            self._spawn(self._configure_awake(self.zdev))

    async def _configure_awake(self, zdev) -> None:
        """Équipement sur pile qui vient d'envoyer : éveillé, il accepte sa configuration
        (sauf si son quirk dit de s'en passer : il envoie ses valeurs de lui-même)."""
        if not zdev.skip_configuration:
            await self._configure(zdev)

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
