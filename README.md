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
  `automations/`, `pairing/` (appairages HomeKit, copie de la base Zigbee).
- **temp/** : 100 % jetable (journaux, base Zigbee vivante), peut être en RAM.
  `equipements.txt`, écrit au démarrage une fois la clé Zigbee initialisée (60 s au plus) : chaque
  équipement et son appareil physique (ou « non appairé »), puis les appareils appairés sans
  équipement déclaré, avec la déclaration à copier dans devices/. S'il y a une anomalie, un
  avertissement dans le journal renvoie à ce fichier.
- **Secrets** (hors dépôt : `../HabSecrets`) : `firebase.json`, clé du compte de service
  Firebase pour le datalog (section `[datalog]` de `habctrl.toml`, voir `doc.md`).

Emplacements modifiables : configuration avec `HABCTRL_CONFIG`, temp/ avec `[paths] temp` de `habctrl.toml`.

## Équipements
Chaque fichier de `devices/` déclare autant d'équipements que voulu (par pièce, par type…) :
toute variable qui est un équipement est chargée, son nom de variable est son identifiant.
```python
salon = YeelightBulb(host="192.168.1.110")
prise_salon = Plug(ieee="a4:c1:38:d6:d7:c6:db:f6")  # Zigbee : adresse IEEE
```
Dans une automatisation : `from devices.salon import salon, prise_salon`.

## Pairing
Onglet **Administration** de l'application web (`/administration`) : un bouton par protocole
(HomeKit : découverte mDNS, défaut 10 s ; Zigbee : réseau ouvert, défaut 120 s, avec la clé de
HabController qui reste en marche), la durée de recherche est réglable. Un seul appairage à la
fois ; fermer la page ou « Annuler » l'interrompt.
L'identifiant de l'équipement est demandé une fois l'appareil identifié ; un équipement déjà
déclaré est gardé, sinon il est ajouté à la fin de `devices/devices.py` avec le pilote déduit du
modèle (`drivers/<protocole>/models.toml`, Zigbee : ou du type d'appareil), à ranger ensuite
librement. Recharger ensuite la configuration (Administration) pour l'utiliser.
HomeKit : un appareil déjà lié propose de garder son identifiant ; un identifiant déjà lié à un
autre appareil le remplace après confirmation (appareil HS).
Zigbee : un appareil déjà déclaré (même IEEE) garde son identifiant ; un identifiant déjà
déclaré est refusé (à corriger à la main dans `devices/`).

### Zigbee : la déclaration fait foi
Chaque équipement Zigbee de `devices/` porte l'adresse IEEE de son appareil (`ieee="…"`),
écrite par l'appairage. Un appareil du réseau qu'aucun équipement déclaré n'utilise (appairage
interrompu, appareil remplacé, déclaration supprimée) n'est jamais retiré automatiquement : il
est listé dans `temp/equipements.txt` (avec la déclaration à copier, si le modèle est connu), et
« Nettoyer l'appairage des équipements non utilisés » (Administration) le retire du réseau après
confirmation. Un équipement dont
l'appareil est absent du réseau est signalé au démarrage (le réappairer). Repartir de zéro :
supprimer `pairing/zigbee.db` et `temp/zigbee.db`, puis réappairer.

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

## Administration : notifications, sauvegarde, mise à jour
Une pastille rouge sur l'onglet **Administration** signale une notification en attente
(`core/notifications.py` : toute partie du programme peut en poser ou en retirer ; texte en
info-bulle, détail sur la page). Notifications des surveillances (`core/watch.py`) :
- **Configuration modifiée** : la date et l'heure du dernier chargement (démarrage ou
  « Recharger la configuration ») sont mémorisées ; un fichier ou un dossier de HabConfig
  (hors `pairing/` et fichiers cachés) modifié après cette date, ou un fichier supprimé, est
  signalé au-dessus de « Recharger la configuration ».
- **Sauvegarde à faire** : HabConfig a des modifications non commitées ou des commits non
  poussés ; « Sauvegarder » commit le tout (message « sauvegarde du <date> ») et pousse.
- **Mise à jour disponible** : la branche suivie de HabController a des commits à tirer
  (recherche une fois par heure, ou « Rechercher une mise à jour ») ; « Mettre à jour et
  redémarrer » fait un `git pull --ff-only` (rien n'est fusionné ni écrasé : en cas de
  divergence, mise à jour refusée), réinstalle les dépendances si `requirements.txt` a changé,
  puis le programme s'arrête proprement et se relance lui-même (même processus : sans sudo ni
  systemd). La page se recharge quand il répond à nouveau. Si le nouveau code plante au
  lancement, systemd le relance (`Restart=on-failure`) au plus 5 fois en 5 min, puis abandonne
  (`StartLimitBurst`) : corriger, puis `sudo systemctl reset-failed habctrl` et
  `sudo systemctl start habctrl`.

Rythme des vérifications : `[watch]` de `habctrl.toml`, `check` (s, défaut 30 : configuration et
sauvegarde) et `fetch` (s, défaut 3600 : mise à jour, réseau).
Git est lancé sans terminal : pousser HabConfig et tirer HabController demandent des
identifiants enregistrés sur la machine (clé SSH, ou `git config credential.helper store` avec
un jeton GitHub), sinon la commande échoue et l'erreur s'affiche.

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
