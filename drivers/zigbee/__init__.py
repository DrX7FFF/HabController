"""Zigbee : coordinateur (clé USB EmberZNet via bellows/zigpy), commun à tous les pilotes Zigbee.

Une seule clé pour tous les équipements : le coordinateur est partagé (`coordinator`), démarré
au premier équipement Zigbee et relancé si la clé décroche. L'appairage l'emprunte
(`coordinator.session()`) : le port série ne s'ouvre qu'une fois."""

import asyncio
import logging
import shutil
import sqlite3
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import zhaquirks
from bellows.ash import AshException
from bellows.zigbee.application import ControllerApplication
from zha.quirks import DEVICE_REGISTRY
from zigpy.application import ControllerApplication as ZigpyApplication
from zigpy.config import (
    CONF_DATABASE,
    CONF_DEVICE,
    CONF_DEVICE_BAUDRATE,
    CONF_DEVICE_PATH,
    CONF_OTA,
    CONF_OTA_EXTRA_PROVIDERS,
    CONF_OTA_PROVIDER_TYPE,
)
from zigpy.device import Device as ZigpyDevice
from zigpy.exceptions import RadioException, ZigbeeException
from zigpy.types import EUI64
from zigpy.zcl import (
    AttributeReportedEvent,
    AttributeUpdatedEvent,
    ClusterType,
    OtaImageAvailableEvent,
    ReportingConfig,
)
from zigpy.zcl.clusters.general import Ota
from zigpy.zcl.foundation import Status

from core import PAIRING_DIR, TEMP_DIR, settings
from core.base import DeviceError, NetworkDevice
from core.firmware import updates

log = logging.getLogger(__name__)

ERRORS = (
    ZigbeeException,
    RadioException,
    AshException,
    OSError,
)  # protocole ou liaison série
DB_FILE = TEMP_DIR / "zigbee.db"  # base vivante, utilisée par zigpy
SNAPSHOT = (
    PAIRING_DIR / "zigbee.db"
)  # copie de sauvegarde de la base, dans la configuration
HOME_AUTOMATION = 0x0104  # profil Zigbee des types d'appareil standard
RETRY_MIN = 2  # s, entre deux tentatives d'ouverture de la clé (doublé à chaque échec)
RETRY_MAX = 60
READY_WAIT = 30  # s, laissées à la clé pour s'ouvrir quand l'appairage l'emprunte
CLOSE_WAIT = 10  # s, au plus, pour arrêter le coordinateur (clé retirée : il peut bloquer)
SAVE_DELAY = 5  # s, laissées à zigpy pour écrire en base la version d'un firmware installé
STOP = 0xFFFF  # intervalle maximal d'envoi spontané : ne plus envoyer


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
        # index Zigbee2MQTT (Koenkk) : firmwares absents des sources par défaut (ex. Philips)
        CONF_OTA: {CONF_OTA_EXTRA_PROVIDERS: [{CONF_OTA_PROVIDER_TYPE: "z2m"}]},
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
    # new() charge la base avant de démarrer : sans elle, les équipements connus sont oubliés.
    # Clé démarrée à part : en cas d'échec, zigpy laisserait la base (et son thread) ouverte.
    app = await ControllerApplication.new(
        config, start_radio=False, device_resolver=await load_quirks()
    )
    try:
        await app.startup(auto_form=True)
        yield app
    finally:
        await close_session(app)


async def close_session(app: ZigpyApplication) -> None:
    """Arrête le coordinateur en CLOSE_WAIT s au plus, puis referme la base dans tous les cas
    (arrêt bloqué ou annulé) : son thread ouvert empêcherait le programme de se terminer."""
    stopping = asyncio.ensure_future(app.shutdown(db=False))
    try:
        await asyncio.wait({stopping}, timeout=CLOSE_WAIT)
        if not stopping.done():
            log.warning(
                "Clé Zigbee : arrêt bloqué depuis %s s, abandonné ; en attente dans :\n%s",
                CLOSE_WAIT,
                asyncio.format_call_graph(stopping),
            )
    finally:
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
        await close_database(app)


async def close_database(app: ZigpyApplication) -> None:
    """Ce que fait shutdown(db=True) après la déconnexion de la clé (attributs internes de
    zigpy : shutdown ne permet pas de fermer la base sans passer par la clé)."""
    if app._dblistener is None:
        return
    app._remove_db_listeners()
    try:
        await asyncio.wait_for(app._dblistener.shutdown(), CLOSE_WAIT)
    except Exception as err:  # base bloquée ou abîmée : on ne peut rien de plus
        log.warning("Base Zigbee : fermeture impossible (%s)", type(err).__name__)


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


def label(device) -> str:
    """IEEE, fabricant et modèle, sur une ligne."""
    return f"{device.ieee} ({device.manufacturer or '?'} {device.model or '?'})"


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
        clusters_out = ", ".join(
            f"0x{cid:04x}" for cid in sorted(endpoint.out_clusters)
        )
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
    return {
        name: value for name, value in success.items() if not isinstance(value, Status)
    }


def tracked(defaults: Mapping, reports: Mapping | None) -> dict:
    """Valeurs suivies {état: écart} : défauts du pilote, remplacés par la déclaration
    (`reports`) ; None : non suivie (absente du résultat)."""
    if unknown := set(reports or {}) - set(defaults):
        raise ValueError(f"reports : {', '.join(sorted(unknown))} inconnu de ce pilote")
    merged = {**defaults, **(reports or {})}
    return {key: change for key, change in merged.items() if change is not None}


def raw_change(change: float) -> int:
    """Écart converti en valeur brute : arrondi, au moins 1."""
    return max(1, round(change))


async def setup_reporting(
    name: str, cluster, reports: dict[str, tuple[int, int, int] | None]
) -> None:
    """Binding vers le coordinateur et envoi spontané des attributs {nom: (min s, max s, écart)}.

    None : ne plus envoyer cet attribut (l'appareil garde sa configuration précédente).
    Facultatif : si l'équipement refuse, l'état reste à jour par la lecture périodique.
    """
    try:
        await cluster.bind()
        result = await cluster.configure_reporting_multiple(
            {
                cluster.find_attribute(attr): ReportingConfig(*(cfg or (0, STOP, 0)))
                for attr, cfg in reports.items()
            }
        )
    except ERRORS as err:
        log.warning(
            "%s : reporting non configuré sur 0x%04x (%s)",
            name,
            cluster.cluster_id,
            err,
        )
        return
    for attr, status in result.items():
        if status != Status.SUCCESS:
            log.warning("%s : reporting de %s refusé (%s)", name, attr.name, status.name)


class Coordinator:
    """La clé partagée : ouverte tant qu'il reste un équipement Zigbee ou un appairage en cours,
    rouverte si elle décroche.

    Reçoit aussi les événements réseau de zigpy (méthodes au nom de l'événement).
    """

    def __init__(self):
        self.app: ZigpyApplication | None = None
        self._devices: dict[EUI64, "ZigbeeDevice"] = {}
        self._sessions = 0  # appairages qui empruntent la clé
        self.paired: set[EUI64] = (
            set()
        )  # appairés depuis le démarrage, déclarés au prochain
        self._task: asyncio.Task | None = None
        self._lost: asyncio.Event | None = None
        self._ready = asyncio.Event()  # clé ouverte

    def register(self, device: "ZigbeeDevice") -> None:
        other = self._devices.setdefault(device.ieee, device)
        if other is not device:
            log.error(
                "%s : IEEE %s déjà utilisée par %s, ignoré",
                device.id,
                device.ieee,
                other.id,
            )
            return
        if self._task is None:
            self._launch()
        elif self.app is not None:
            device._attach(self.app)

    async def unregister(self, device: "ZigbeeDevice") -> None:
        if self._devices.get(device.ieee) is device:
            del self._devices[device.ieee]
        await self._release()

    @property
    def idle(self) -> bool:
        """Clé jamais demandée (aucun équipement Zigbee déclaré, pas d'appairage)."""
        return self._task is None

    async def wait_ready(self) -> None:
        """Rend la main quand la clé est ouverte."""
        await self._ready.wait()

    def unused(self, app: ZigpyApplication) -> list:
        """Appareils du réseau qu'aucun équipement déclaré n'utilise (hors appairés depuis le
        démarrage : leur déclaration n'est pas encore chargée)."""
        used = self._devices.keys() | self.paired
        return [zdev for zdev in joined_devices(app) if zdev.ieee not in used]

    async def remove_unused(self) -> list[str]:
        """Retire du réseau les appareils inutilisés ; renvoie la description des retirés."""
        removed = []
        async with self.session() as app:
            for zdev in self.unused(app):
                try:
                    await app.remove(zdev.ieee)
                except ERRORS as err:
                    log.warning(
                        "Zigbee : retrait de %s impossible (%s)", label(zdev), err
                    )
                    continue
                log.info("Zigbee : %s retiré du réseau", label(zdev))
                removed.append(label(zdev))
        if removed:
            save_snapshot()
        return removed

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ZigpyApplication]:
        """La clé ouverte, pour l'appairage (ouverte pour l'occasion si besoin)."""
        self._sessions += 1
        try:
            if self._task is None or self._task.done():
                self._launch()
            ready = asyncio.ensure_future(self._ready.wait())
            await asyncio.wait(
                {ready, self._task},
                timeout=READY_WAIT,
                return_when=asyncio.FIRST_COMPLETED,
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
            log.warning("%s : a quitté le réseau Zigbee", device.id)
            device._set_connected(False)


coordinator = Coordinator()


def undeclared() -> list[tuple[str, list[str], str, tuple[str, ...]]] | None:
    """Appareils du réseau qu'aucun équipement n'utilise : (description, clés de models.toml,
    commentaire de déclaration, arguments de déclaration) pour chacun ; None si la clé
    demandée n'est pas ouverte."""
    app = coordinator.app
    if app is None:
        return [] if coordinator.idle else None
    return [
        (
            f"Zigbee {zdev.ieee}",
            model_keys(zdev),
            f"Zigbee : {zdev.manufacturer or '?'}, modèle {zdev.model or '?'}",
            (f'ieee="{zdev.ieee}"',),
        )
        for zdev in coordinator.unused(app)
    ]


async def ready() -> None:
    """Rend la main quand undeclared() connaît les appareils : clé ouverte, ou jamais demandée."""
    if not coordinator.idle:
        await coordinator.wait_ready()


class ZigbeeDevice(NetworkDevice):
    """Base des pilotes Zigbee : rattachement au coordinateur, lecture périodique, joignabilité.

    Le pilote décrit son équipement par trois méthodes : `_configure` (binding et reporting,
    à chaque rattachement), `_read` (lecture complète de l'état, lève une erreur de ERRORS si
    l'équipement ne répond pas) et `_on_attribute` (valeur reçue spontanément).
    L'équipement est joignable tant qu'il répond : une lecture toutes les `poll` secondes le
    vérifie (et rattrape les valeurs que l'équipement n'envoie pas de lui-même).
    Équipement sur pile (POLL = None) : il dort, on ne l'interroge jamais. Il est joignable dès
    son premier message, et configuré à ce moment-là (il est éveillé) ; pas de `_read`.
    L'appareil physique est désigné par son adresse IEEE (`ieee`, ex. "00:17:88:01:08:9d:f1:10").
    """

    POLL: float | None = 300  # s, période de lecture par défaut ; None : sur pile

    def __init__(self, ieee: str, poll: float | None = None):
        super().__init__()
        try:
            self.ieee = EUI64.convert(ieee)
        except (AssertionError, ValueError) as err:
            raise ValueError(f"adresse IEEE invalide « {ieee} »") from err
        self.poll = self.POLL if poll is None else poll
        self.zdev = None  # objet zigpy de l'équipement, tant que la clé est ouverte
        self._unsubscribe: list = []
        self._tasks: set[asyncio.Task] = set()
        self._ota_image = None  # firmware plus récent trouvé par zigpy, à installer à la demande

    # --- cycle de vie -------------------------------------------------------

    async def start(self) -> None:
        coordinator.register(self)

    def physical(self) -> str:
        """Adresse IEEE, et version du firmware si zigpy la connaît (équipement à mise à jour OTA)."""
        try:
            ota = self.zdev.find_cluster(Ota.cluster_id, ClusterType.Client)
            version = ota.get("current_file_version")
        except (AttributeError, ValueError):  # clé fermée, ou pas de mise à jour OTA
            version = None
        return f"Zigbee {self.ieee}" + (f" (firmware 0x{version:08x})" if version else "")

    async def stop(self) -> None:
        updates.withdraw(self.id)
        await coordinator.unregister(self)
        self._detach()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (err := task.exception()):
            log.error("%s : erreur inattendue", self.id, exc_info=err)

    def _attach(self, app: ZigpyApplication) -> None:
        """Clé ouverte ou équipement réinterviewé : écoute et lecture de l'objet zigpy courant."""
        self._detach()
        zdev = app.devices.get(self.ieee)
        if not isinstance(zdev, ZigpyDevice):
            log.error(
                "%s : %s inconnu du réseau Zigbee (l'appairer)", self.id, self.ieee
            )
            return
        self.zdev = zdev
        for endpoint_id, endpoint in zdev.endpoints.items():
            if endpoint_id == 0:
                continue
            # clusters de sortie aussi : certains capteurs y rapportent (ex. porte Xiaomi, 0x0006)
            for cluster in (
                *endpoint.in_clusters.values(),
                *endpoint.out_clusters.values(),
            ):
                # rapport spontané de l'équipement (attribute_report), ou valeur corrigée par
                # un quirk (attribute_updated) : zigpy n'émet qu'un des deux par valeur reçue
                for event in (AttributeReportedEvent, AttributeUpdatedEvent):
                    self._unsubscribe.append(
                        cluster.on_event(event.event_type, self._on_event)
                    )
                self._unsubscribe.append(
                    cluster.on_event(OtaImageAvailableEvent.event_type, self._on_ota)
                )
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
                    log.warning("%s : ne répond plus (%s)", self.id, type(err).__name__)
                self._set_connected(False)
            else:
                if not self._connected:
                    log.info("%s : connecté", self.id)
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
        if (
            event.attribute_name is None
        ):  # attribut propre au fabricant, inconnu de zigpy
            return
        self._received()
        self._on_attribute(event.cluster_id, event.attribute_name, event.value)
        self._notify()

    def _on_ota(self, event: OtaImageAvailableEvent) -> None:
        """Recherche de firmware de zigpy : le plus récent est proposé (rien n'est installé)."""
        if not event.images_result.upgrades:
            self._ota_image = None
            updates.withdraw(self.id)
            return
        self._ota_image = max(event.images_result.upgrades, key=lambda image: image.version)
        current = event.query_cmd.current_file_version
        updates.offer(self, f"0x{current:08x}", f"0x{self._ota_image.version:08x}")

    async def update_firmware(self, progress) -> None:
        """Installe le firmware proposé (progress(%) pendant l'envoi), puis copie la base, qui
        porte la nouvelle version, dans la configuration. DeviceError en cas d'échec."""
        if self.zdev is None or self._ota_image is None:
            raise DeviceError(f"{self.id} : injoignable")
        try:
            status = await self.zdev.update_firmware(
                self._ota_image, lambda _sent, _total, percent: progress(percent)
            )
        except (*ERRORS, TimeoutError) as err:
            raise DeviceError(f"{self.id} : envoi interrompu ({type(err).__name__})") from err
        if status != Status.SUCCESS:
            raise DeviceError(f"{self.id} : firmware refusé ({getattr(status, 'name', status)})")
        self._ota_image = None
        await asyncio.sleep(SAVE_DELAY)
        save_snapshot()

    def _received(self) -> None:
        """Message reçu : un équipement sur pile devient joignable (et se configure)."""
        if not self.poll and not self._connected and self.zdev is not None:
            log.info("%s : connecté", self.id)
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
            raise DeviceError(f"{self.id} : injoignable")
        try:
            result = await getattr(cluster, command)(*args)
        except ERRORS as err:
            self._set_connected(False)
            raise DeviceError(f"{self.id} : {type(err).__name__}") from err
        status = getattr(result, "status", Status.SUCCESS)
        if status != Status.SUCCESS:
            raise DeviceError(f"{self.id} : commande {command} refusée ({status.name})")
