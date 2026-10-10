import asyncio
import logging

from bellows.zigbee.application import ControllerApplication
from zigpy.config import CONF_DEVICE, CONF_DEVICE_PATH

SERIAL_PORT = "/dev/ttyUSB0"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ZigbeeScanner")


# --- CLASSE D'ÉCOUTE DES ÉVÉNEMENTS ---
class ZigbeeNetworkListener:
    """Gère les événements du réseau Zigbee en direct."""

    def device_initialized(self, device):
        """Appelé quand un appareil a terminé son interview complète."""
        logger.info("\n🎉 [INFO] Appareil INITIALISÉ avec succès !")
        logger.info(f"  - IEEE : {device.ieee}")
        logger.info(f"  - NWK  : 0x{device.nwk:04x}")
        logger.info(f"  - Modèle : {getattr(device, 'model', 'Inconnu')}")
        logger.info(f"  - Fabricant : {getattr(device, 'manufacturer', 'Inconnu')}\n")

    def device_joined(self, device):
        """Appelé dès qu'un appareil physique commence à s'associer."""
        logger.info(
            f"\n📡 [SCAN] Un appareil tente de rejoindre le réseau (IEEE: {device.ieee}). Interview en cours..."
        )

    def device_left(self, device):
        """Appelé si un appareil quitte le réseau."""
        logger.info(f"❌ [INFO] L'appareil {device.ieee} a quitté le réseau.")


async def main():
    config = {CONF_DEVICE: {CONF_DEVICE_PATH: SERIAL_PORT, "baudrate": 115200}}

    logger.info(f"Connexion au coordinateur sur {SERIAL_PORT}...")
    app = ControllerApplication(config)

    # Démarrage avec création auto si besoin
    await app.startup(auto_form=True)

    # Enregistrement de notre écouteur d'événements
    listener = ZigbeeNetworkListener()
    app.add_listener(listener)

    logger.info("\n--- LISTE DES ÉQUIPEMENTS ZIGBEE ENREGISTRÉS ---")
    if not app.devices:
        print("Aucun appareil trouvé dans la base de données locale.")
    else:
        for ieee, device in app.devices.items():
            if device.nwk == 0x0000:
                print(f"[Coordinateur] IEEE: {ieee} | NWK: 0x{device.nwk:04x}")
                continue
            print(
                f"[Appareil] IEEE: {ieee} | NWK: 0x{device.nwk:04x} | {getattr(device, 'manufacturer', 'Inconnu')} - {getattr(device, 'model', 'Inconnu')}"
            )

    # Ouverture du réseau (Durée : 60 secondes)
    logger.info("\n[Scanner] Ouverture du réseau pendant 60 secondes (Permit Join)...")
    await app.permit(60)

    # Attente active pendant le scan
    await asyncio.sleep(60)

    # Fermeture propre
    logger.info("Fermeture du script Zigbee.")
    await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrompu par l'utilisateur.")
