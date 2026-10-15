# HabController
Habitation domotique home mad

## Start
./habctrl

## Organisation
- **Application** (ce dépôt) : `core/` (moteur), `drivers/<protocole>/` (homekit, zigbee,
  yeelight : pilotes, appairage, modèles connus), `main.py` (serveur web).
- **Configuration** (dépôt privé HabConfig, cloné à côté : `../HabConfig`) : tout ce qui décrit
  l'installation et suffit à la restaurer — `habctrl.toml` (réglages), `dashboard/`, `devices/`,
  `automations/`, `pairing/` (appairages HomeKit, correspondance et copie de la base Zigbee).
- **temp/** : 100 % jetable (journaux, base Zigbee vivante), peut être en RAM.
  `equipements.txt`, écrit au démarrage : chaque équipement et son appareil physique (ou « non
  appairé »), puis les appareils liés à un équipement non déclaré, avec la déclaration à copier
  dans devices/. S'il y a une anomalie, un seul avertissement dans le journal renvoie à ce fichier.
- **Secrets** (hors dépôt : `../HabSecrets`) : `firebase.json`, clé du compte de service
  Firebase pour le datalog (section `[datalog]` de `habctrl.toml`, voir `core/datalog.py`).

Emplacements modifiables avec `HABCTRL_CONFIG` et `HABCTRL_TEMP`.

## Équipements
Chaque fichier de `devices/` déclare autant d'équipements que voulu (par pièce, par type…) :
toute variable qui est un équipement est chargée, son nom de variable est son identifiant.
```python
salon = YeelightBulb(host="192.168.1.110", name="Salon")
prise_salon = Plug(name="Prise salon")
```
Dans une automatisation : `from devices.salon import salon, prise_salon`.

## Pairing
```batch
./habctrl pair -h                                  # protocoles disponibles
./habctrl pair homekit                             # --duration : découverte mDNS (défaut 10 s)
./habctrl pair zigbee                              # HabController arrêté (il occupe la clé)
```
Le nom de l'équipement est demandé une fois l'appareil identifié (l'identifiant en est
déduit) ; un appareil déjà lié propose de garder son nom. Un nom déjà lié à un autre appareil
le remplace après confirmation (appareil HS) ; un équipement déjà déclaré est gardé, sinon il
est ajouté à la fin de `devices/devices.py` avec le pilote déduit du modèle
(`drivers/<protocole>/models.toml`, Zigbee : ou du type d'appareil), à ranger ensuite
librement. Redémarrer HabController ensuite.

### Zigbee : `pairing/zigbee.toml` fait foi
Une ligne par appareil : `"<IEEE>" = "<id>"`. À chaque ouverture de la clé (démarrage,
appairage), les appareils de la base absents de ce fichier sont retirés du réseau : un
appareil remplacé disparaît, un appairage perdu se refait simplement. Un lien vers un appareil
absent du réseau est signalé (décalage avec la base). Fichier absent ou illisible : rien n'est retiré. Repartir de zéro : supprimer
`pairing/zigbee.toml`, `pairing/zigbee.db` et `temp/zigbee.db`.

## Restauration complète
Cloner l'application et la configuration côte à côte, brancher la clé Zigbee, lancer. La base Zigbee est
reprise de `pairing/zigbee.db` (écrite à chaque appairage) ; les appairages
HomeKit restent valables tant que les accessoires n'ont pas été réinitialisés.

## détection clé USB SonOff Dongle Plus MG24
❯ lsusb | grep -i "Silicon"
Bus 001 Device 015: ID 10c4:ea60 Silicon Labs CP210x UART Bridge

❯ ls -l /dev/serial/by-id/
total 0
lrwxrwxrwx 1 root root 13 oct.  10 23:08 usb-SONOFF_SONOFF_Dongle_Plus_MG24_cccd71bbb69aef1193e6b89061ce3355-if00-port0 -> ../../ttyUSB0
