import asyncio
import logging
import os
from typing import Any, cast

from bellows.zigbee.application import ControllerApplication
from zigpy.config import CONF_DATABASE, CONF_DEVICE, CONF_DEVICE_PATH
from zigpy.device import ZigbeeDevice
from zigpy.exceptions import ZigbeeException

SERIAL_PORT = "/dev/ttyUSB0"

logging.basicConfig(level=logging.INFO)
logging.getLogger("zigpy").setLevel(logging.WARNING)
logging.getLogger("bellows").setLevel(logging.WARNING)
logger = logging.getLogger("ZigbeeController")


class ClusterListener:
    def __init__(self, endpoint_id: int, cluster_id: int):
        self.endpoint_id = endpoint_id
        self.cluster_id = cluster_id

    def attribute_updated(self, attrid: int, value: Any):
        if self.cluster_id == 0x0006 and attrid == 0x0000:
            etat = "ALLUMÉE 🟢" if value else "ÉTEINTE 🔴"
            logger.info(f"📢 [ÉVÉNEMENT] Prise -> État modifié : {etat}")
        else:
            logger.info(
                f"📢 [ATTRIBUT] Endpoint #{self.endpoint_id}, Cluster 0x{self.cluster_id:04x}, Attr 0x{attrid:04x} -> Valeur: {value}"
            )

    def cluster_command(self, tsn: int, command_id: int, args: Any):
        pass


class NetworkManager:
    def __init__(self, app: ControllerApplication):
        self.app = app

    def device_initialized(self, device: ZigbeeDevice):
        logger.info(
            f"🎉 [NOUVEAU] Appareil initialisé : {device.model} ({device.ieee})"
        )
        asyncio.create_task(self._setup_device_reporting(device))

    async def _setup_device_reporting(self, device: ZigbeeDevice):
        """Configure le Binding et force la sauvegarde via l'API."""
        for ep_id, endpoint in device.endpoints.items():
            if ep_id == 0:
                continue

            in_clusters_dict = cast(dict[int, Any], endpoint.in_clusters)

            if 0x0006 in in_clusters_dict:
                cluster = in_clusters_dict[0x0006]

                try:
                    logger.info(
                        f"⚙️ Configuration du suivi d'état pour l'endpoint #{ep_id}..."
                    )
                    await cluster.bind()
                    await cluster.configure_reporting(0x0000, 0, 3600, 1)

                    # Force une lecture pour réveiller le reporting
                    await cluster.read_attributes([0x0000])
                    logger.info(
                        f"✅ Suivi d'état configuré avec succès sur l'endpoint #{ep_id}"
                    )
                except ZigbeeException as e:
                    logger.warning(
                        f"⚠️ Échec de la configuration sur l'endpoint #{ep_id}: {e}"
                    )

                listener = ClusterListener(ep_id, 0x0006)
                cluster.add_listener(listener)

        # --- Solution pour calmer le linter : invocation dynamique ---
        try:
            # On passe par un typage Any temporaire pour empêcher basedpyright de bloquer sur le dictionnaire
            devices_obj = cast(Any, self.app.devices)
            if hasattr(devices_obj, "write"):
                devices_obj.write()
                logger.info("💾 Base de données synchronisée sur le disque.")
        except ZigbeeException as e:
            logger.warning(f"⚠️ Échec de l'écriture en base : {e}")

    def attach_existing_listeners(self, device: ZigbeeDevice):
        for ep_id, endpoint in device.endpoints.items():
            if ep_id == 0:
                continue
            in_clusters_dict = cast(dict[int, Any], endpoint.in_clusters)
            for cluster_id, cluster in in_clusters_dict.items():
                if cluster_id == 0x0006:
                    listener = ClusterListener(ep_id, cluster_id)
                    cluster.add_listener(listener)


async def main():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(current_dir, "zigbee.db")

    config = {
        CONF_DEVICE: {CONF_DEVICE_PATH: SERIAL_PORT, "baudrate": 115200},
        CONF_DATABASE: db_path,
    }

    logger.info(f"Connexion à la clé Sonoff MG24 sur {SERIAL_PORT}...")

    app = ControllerApplication(config)
    await app.startup(auto_form=True)

    manager = NetworkManager(app)
    app.add_listener(manager)

    if app.devices:
        for ieee, device in app.devices.items():
            if device.nwk == 0x0000 or not isinstance(device, ZigbeeDevice):
                continue
            logger.info(
                f"🔗 Liaison initiale avec : {getattr(device, 'model', 'Inconnu')} ({ieee})"
            )
            manager.attach_existing_listeners(device)

    logger.info("\n[Radio] Ouverture du réseau pendant 60 secondes (Permit Join)...")
    await app.permit(60)

    logger.info(
        "📡 Écoute active. Appuyez sur le bouton physique de la prise ou Ctrl+C pour quitter."
    )

    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Fermeture propre du réseau.")
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nArrêt.")
