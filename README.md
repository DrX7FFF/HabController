# HabController
Habitation domotique home mad

## Start
./habctrl

## Organisation
- **Application** (ce dépôt) : `core/` (moteur), `drivers/<protocole>/` (homekit, zigbee,
  yeelight : pilotes, appairage, modèles connus), `main.py` (serveur web), `web/` (pages :
  `/` commandes, `/graphiques` historique du datalog décrit par `dashboard/charts.toml` ;
  `web/plot.js` est le seul fichier lié à la bibliothèque de tracé, uPlot dans `web/vendor/`).
- **Configuration** (dépôt privé HabConfig, cloné à côté : `../HabConfig`) : tout ce qui décrit
  l'installation et suffit à la restaurer — `habctrl.toml` (réglages), `dashboard/`, `devices/`,
  `automations/`, `pairing/` (appairages HomeKit, correspondance et copie de la base Zigbee).
- **temp/** : 100 % jetable (journaux, base Zigbee vivante), peut être en RAM.
  `equipements.txt`, écrit au démarrage : chaque équipement et son appareil physique (ou « non
  appairé »), puis les appareils liés à un équipement non déclaré, avec la déclaration à copier
  dans devices/. S'il y a une anomalie, un seul avertissement dans le journal renvoie à ce fichier.
- **Secrets** (hors dépôt : `../HabSecrets`) : `firebase.json`, clé du compte de service
  Firebase pour le datalog (section `[datalog]` de `habctrl.toml`, voir `doc.md`).

Emplacements modifiables avec `HABCTRL_CONFIG` et `HABCTRL_TEMP`.

## Équipements
Chaque fichier de `devices/` déclare autant d'équipements que voulu (par pièce, par type…) :
toute variable qui est un équipement est chargée, son nom de variable est son identifiant.
```python
salon = YeelightBulb(host="192.168.1.110")
prise_salon = Plug()
```
Dans une automatisation : `from devices.salon import salon, prise_salon`.

## Pairing
Onglet **Administration** de l'application web (`/administration`) : un bouton par protocole
(HomeKit : découverte mDNS, défaut 10 s ; Zigbee : réseau ouvert, défaut 120 s, avec la clé de
HabController qui reste en marche), la durée de recherche est réglable. Un seul appairage à la
fois ; fermer la page ou « Annuler » l'interrompt.
L'identifiant de l'équipement est demandé une fois l'appareil identifié ; un appareil déjà
lié propose de garder son identifiant. Un identifiant déjà lié à un autre appareil le remplace
après confirmation (appareil HS) ; un équipement déjà déclaré est gardé, sinon il
est ajouté à la fin de `devices/devices.py` avec le pilote déduit du modèle
(`drivers/<protocole>/models.toml`, Zigbee : ou du type d'appareil), à ranger ensuite
librement. Redémarrer HabController ensuite pour l'utiliser.

### Zigbee : `pairing/zigbee.toml` fait foi
Une ligne par appareil : `"<IEEE>" = "<id>"`. À chaque ouverture de la clé (démarrage,
appairage), les appareils de la base absents de ce fichier sont retirés du réseau : un
appareil remplacé disparaît, un appairage perdu se refait simplement. Un lien vers un appareil
absent du réseau est signalé (décalage avec la base). Fichier absent ou illisible : rien n'est retiré. Repartir de zéro : supprimer
`pairing/zigbee.toml`, `pairing/zigbee.db` et `temp/zigbee.db`.

### Zigbee : zha-quirks pour les appareils non standard
Seuls zigpy et bellows (clé EmberZNet) sont nécessaires au Zigbee. zha-quirks est ajouté par
choix, pour la compatibilité des appareils qui s'écartent de la norme (Xiaomi, Tuya, Philips
Hue Dimmer…) : ses correctifs sont appliqués par zigpy au chargement de la base et à
l'appairage, et les pilotes ne lisent que des clusters standard. Il entraîne `zha` (bibliothèque
de Home Assistant), qui fixe les versions exactes de zigpy et bellows, et d'autres paquets
inutilisés ici (pilotes d'autres clés). Pour s'en passer : retirer `zha-quirks` de
`requirements.txt` et `device_resolver` de `open_session()` (`drivers/zigbee/__init__.py`),
puis réécrire dans chaque pilote ce que son correctif apportait (ex. TS011F : diviseurs 100 pour
l'énergie, 1000 pour le courant, que la prise n'annonce pas).

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

## Mise en mode appairage des équipements
### Philips Hue LWA001 (ampoule E27 Bluetooth)
Avec une télécommande Hue Dimmer (méthode qui a fonctionné) :
1. couper puis rallumer l'ampoule (peut échouer si elle est allumée depuis longtemps) ;
2. tenir la télécommande le plus près possible de l'ampoule ;
3. appuyer ensemble sur I/On et 0/Off 10 à 12 s, jusqu'au clignotement de l'ampoule ;
4. garder appuyé jusqu'au dernier clignotement, plus une seconde ;
5. couper puis rallumer l'ampoule : elle peut être appairée.

Source : [Zigbee2MQTT, Philips 8718699673147](https://www.zigbee2mqtt.io/devices/8718699673147.html).
