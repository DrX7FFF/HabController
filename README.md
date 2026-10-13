# HabController
Habitation domotique home mad

## Start
./habctrl

## Organisation
- **Application** (ce dépôt) : `core/` (moteur), `drivers/<protocole>/` (homekit, zigbee,
  yeelight : pilotes, appairage, modèles connus), `main.py` (serveur web).
- **Configuration** (dépôt privé HabConfig, cloné à côté : `../HabConfig`) : tout ce qui décrit
  l'installation et suffit à la restaurer — `habctrl.toml` (réglages), `dashboard/`, `devices/`,
  `automations/`, `pairing/` (appairages HomeKit et instantané de la base Zigbee).
- **temp/** : 100 % jetable (journaux, base Zigbee vivante), peut être en RAM.

Emplacements modifiables avec `HABCTRL_CONFIG` et `HABCTRL_TEMP`.

## Pairing
```batch
./habctrl pair -h                                  # protocoles disponibles
./habctrl pair homekit fp2
./habctrl pair zigbee prise_salon --duration 120   # HabController arrêté (il occupe la clé)
```
Le pilote est déduit du modèle (`drivers/<protocole>/models.toml`) et `devices/<id>.py`
est créé. Redémarrer HabController ensuite.

## Restauration complète
Cloner l'application et la configuration côte à côte, brancher la clé Zigbee, lancer. La base Zigbee est
reprise de `pairing/zigbee.db` (écrite à l'arrêt et une fois par jour) ; les appairages
HomeKit restent valables tant que les accessoires n'ont pas été réinitialisés.

## détection clé USB SonOff Dongle Plus MG24
❯ lsusb | grep -i "Silicon"
Bus 001 Device 015: ID 10c4:ea60 Silicon Labs CP210x UART Bridge

❯ ls -l /dev/serial/by-id/
total 0
lrwxrwxrwx 1 root root 13 oct.  10 23:08 usb-SONOFF_SONOFF_Dongle_Plus_MG24_cccd71bbb69aef1193e6b89061ce3355-if00-port0 -> ../../ttyUSB0
