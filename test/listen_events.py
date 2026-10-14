import asyncio
import logging
import os
from typing import Any, cast

# CORRECTION IMPORT : On utilise la classe de base officielle demandée par le linter
from zigpy.application import ControllerApplication
from zigpy.config import CONF_DEVICE, CONF_DEVICE_BAUDRATE, CONF_DEVICE_PATH
from zigpy.device import ZigbeeDevice

SERIAL_PORT = "/dev/ttyUSB0"

logging.basicConfig(level=logging.INFO)
logging.getLogger("zigpy").setLevel(logging.WARNING)
logging.getLogger("bellows").setLevel(logging.WARNING)
logger = logging.getLogger("ZigbeeController")


class GlobalPacketListener:
    def __init__(self, app: ControllerApplication):
        self.app = app

    def handle_message(
        self,
        sender: ZigbeeDevice,
        profile: int,
        cluster: int,
        src_ep: int,
        dst_ep: int,
        message: Any,
    ):
        """Intercepte tous les messages radio bruts."""
        if cluster == 0x0006:
            payload = getattr(message, "data", b"")
            if payload:
                dernier_octet = payload[-1]
                if dernier_octet in (0, 1):
                    etat = "ALLUMÉE 🟢" if dernier_octet == 1 else "ÉTEINTE 🔴"
                    logger.info(f"📢 [RADIO] Prise ({sender.ieee}) -> État détecté : {etat}")
                else:
                    logger.info(f"📦 [BRUT] Cluster OnOff reçu, Payload: {payload.hex()}")


class NetworkManager:
    def __init__(self, app: ControllerApplication):
        self.app = app

    def device_initialized(self, device: ZigbeeDevice):
        """Se déclenche automatiquement si vous appairez un nouvel appareil en direct."""
        logger.info(f"🎉 [NOUVEAU] Appareil détecté et initialisé : {device.model} ({device.ieee})")
        asyncio.create_task(self._force_binding(device))

    async def _force_binding(self, device: ZigbeeDevice):
        """Force l'abonnement aux rapports d'état."""
        for ep_id, endpoint in device.endpoints.items():
            if ep_id == 0:
                continue
            in_clusters_dict = cast(dict[int, Any], endpoint.in_clusters)
            if 0x0006 in in_clusters_dict:
                try:
                    cluster = in_clusters_dict[0x0006]
                    await cluster.bind()
                    await cluster.configure_reporting(0x0000, 0, 3600, 1)
                    await cluster.read_attributes([0x0000])
                except Exception:
                    pass


async def main():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(current_dir, "zigbee.db")

    config = {
        "database_path": db_path,
        CONF_DEVICE: {CONF_DEVICE_PATH: SERIAL_PORT, CONF_DEVICE_BAUDRATE: 115200},
        "ota": {
            "ikea_provider": False,
            "ledvance_provider": False,
            "salus_provider": False,
            "sonoff_provider": False,
            "tuya_provider": False,
        },
    }

    logger.info(f"Connexion au coordinateur sur {SERIAL_PORT}...")

    # Appel de l'API bellows spécifié de manière transparente via la clé 'device' de zigpy
    app = await ControllerApplication.new(config=config, auto_form=True)

    # Enregistrement des écouteurs
    packet_listener = GlobalPacketListener(app)
    app.add_listener(packet_listener)

    manager = NetworkManager(app)
    app.add_listener(manager)

    logger.info("🔗 Liaison avec les appareils enregistrés...")
    for ieee, device in app.devices.items():
        if device.nwk == 0x0000 or not isinstance(device, ZigbeeDevice):
            continue
        logger.info(f"🎯 Appareil en mémoire : {getattr(device, 'model', 'Inconnu')} ({ieee})")
        asyncio.create_task(manager._force_binding(device))

    # On ouvre la radio pendant 30 secondes pour stabiliser la session à chaud
    logger.info("\n[Radio] Ouverture du réseau (Permit Join) pendant 30s...")
    await app.permit(30)

    logger.info("📡 Écoute active en continu. Utilisez votre prise ou Ctrl+C pour quitter.")

    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Fermeture propre du port série.")
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nArrêt.")
