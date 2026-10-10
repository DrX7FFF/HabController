import asyncio
import logging
import os

from bellows.zigbee.application import ControllerApplication
from zigpy.config import CONF_DEVICE, CONF_DEVICE_BAUDRATE, CONF_DEVICE_PATH
from zigpy.device import ZigbeeDevice

SERIAL_PORT = "/dev/ttyUSB0"

logging.basicConfig(level=logging.INFO)
logging.getLogger("zigpy").setLevel(logging.INFO)
logging.getLogger("bellows").setLevel(logging.WARNING)
logger = logging.getLogger("ZigbeePairing")


class PairingManager:
    def device_initialized(self, device: ZigbeeDevice):
        logger.info(f"\n🎉 [SUCCÈS] Appareil appairé : {device.model} ({device.ieee})")


async def main():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(current_dir, "zigbee.db")

    # Dictionnaire complet pour satisfaire le validateur voluptuous
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

    # Initialisation de l'application
    app = await ControllerApplication.new(config=config, auto_form=True)

    manager = PairingManager()
    app.add_listener(manager)

    logger.info(f"\n📁 Fichier sqlite ciblé : {db_path}")
    logger.info("[Radio] Ouverture du réseau pendant 60 secondes (Permit Join)...")
    logger.info("👉 Activez le mode appairage sur votre prise.")
    await app.permit(60)

    await asyncio.sleep(60)

    logger.info("\n💾 Clôture de la session...")
    await app.shutdown()
    logger.info("✅ Session fermée proprement.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nArrêt.")
