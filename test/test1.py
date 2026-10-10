import asyncio
import logging
import time

from bellows.zigbee.application import ControllerApplication
from zigpy.config import CONF_DEVICE, CONF_DEVICE_PATH

# Configuration du port USB
SERIAL_PORT = "/dev/ttyUSB0"

# Activation des logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ZigbeeScanner")


async def main():
    # 1. Configuration pour le protocole Ember (Silabs MG24)
    config = {CONF_DEVICE: {CONF_DEVICE_PATH: SERIAL_PORT, "baudrate": 115200}}

    logger.info(f"Connexion au coordinateur sur {SERIAL_PORT}...")
    app = ControllerApplication(config)

    # 2. Démarrage (auto_form=True crée le réseau s'il est manquant)
    await app.startup(auto_form=True)

    logger.info("\n--- LISTE DES ÉQUIPEMENTS ZIGBEE ENREGISTRÉS ---")

    # 3. Parcours et affichage des appareils connus
    if not app.devices:
        print("Aucun appareil trouvé dans la base de données du coordinateur.")
    else:
        for ieee, device in app.devices.items():
            # Identification du coordinateur
            if device.nwk == 0x0000:
                print(f"\n[Coordinateur] IEEE: {ieee} | NWK: 0x{device.nwk:04x}")
                continue

            print(f"\n[Appareil] Adresse IEEE : {ieee}")
            print(f"  - Adresse Réseau (NWK) : 0x{device.nwk:04x}")
            print(
                f"  - Fabricant/Modèle : {getattr(device, 'manufacturer', 'Inconnu')} - {getattr(device, 'model', 'Inconnu')}"
            )

            # Gestion de l'état de présence via last_seen
            if device.last_seen:
                date_str = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(device.last_seen)
                )
                print(f"  - Dernière activité : {date_str}")

                # Exemple : on considère en ligne si vu il y a moins de 2 heures
                if time.time() - device.last_seen < 7200:
                    print("  - État estimé : En ligne")
                else:
                    print("  - État estimé : Inactif (Hors ligne)")
            else:
                print("  - Dernière activité : Jamais vu")
                print("  - État estimé : Inconnu")

    # 4. Ouverture du réseau pendant 60 secondes (syntaxe propre à zigpy)
    logger.info("\n[Scanner] Ouverture du réseau pendant 60 secondes (Permit Join)...")
    await app.permit(60)

    # Maintien du script en vie pendant le scan
    await asyncio.sleep(60)

    # Fermeture propre du port série
    logger.info("Fermeture du script Zigbee.")
    await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrompu par l'utilisateur.")
