# Documentation HabController

Organisation des dépôts, déclaration des équipements et appairage : voir `README.md`.

## Automatisations

Fonctions déclenchées par les changements d'état des équipements. Un fichier par sujet dans
`automations/`, chargé au démarrage. Les équipements s'importent depuis le fichier de
`devices/` qui les déclare :

```python
import logging

from core.automation import Event, on_change
from devices.salon import capteur, salon
from devices.cuisine import cuisine

log = logging.getLogger(__name__)  # journal des scripts : temp/automations.log

@on_change("capteur.presence")            # « équipement.attribut » ; « capteur » = tout l'état
async def eclairage(event: Event):        # event.old, event.new, event.changed, event.time
    if capteur.connected and capteur.presence:
        salon.on = True                   # écriture : commande envoyée en tâche de fond
        cuisine.on = True
```

- **Lecture** : un attribut vaut `None` si l'équipement est injoignable (l'attribut disparaît
  de son état).
- **Écriture** : `salon.on = True` ne bloque pas et ne lève rien si l'équipement est
  injoignable (c'est journalisé) ; les commandes d'un même équipement partent dans l'ordre.
  Pour attendre la réponse, ou passer une transition : `ok = await salon.send("on", True, 2)`,
  qui renvoie `False` si la commande n'a pas pu partir.
- `to=` ne déclenche que vers une valeur donnée : `@on_change("capteur.presence", to=True)`.
- **Événements ponctuels** (boutons) : `@on_event("bouton.double")` à chaque double appui, ou
  `@on_event("bouton")` pour tous ; `event.attr` est le nom de l'événement, `event.new` sa
  valeur éventuelle (ex. angle de rotation du cube), `event.old` toujours `None`.
- Un attribut absent de l'état (équipement injoignable) n'est pas un changement : on garde la
  dernière valeur connue, et rien ne se déclenche tant que la valeur revient identique.
- Les événements d'une même fonction sont traités dans l'ordre ; une erreur dans une
  automatisation n'arrête pas les autres.
- Un équipement inconnu dans un `@on_change` est signalé au démarrage.
- Un fichier qui échoue au chargement (erreur de syntaxe, import faux…) est déchargé en entier,
  y compris les automatisations déclarées avant l'erreur ; l'erreur est journalisée et les
  autres fichiers se chargent normalement.

## Journaux

- Système (pilotes, connexions, serveur, moteur d'automatisation) : console + `temp/system.log`.
- Scripts (`automations/*.py`) : `temp/automations.log` seulement, jamais dans la console.

Un script se journalise avec `log = logging.getLogger(__name__)` : son nom
(`automations.<script>`) suffit à l'envoyer dans le bon fichier.

## Datalog

Historique des valeurs dans Firebase (Realtime Database). Schéma :
`<chemin>/<horodatage ms> : valeur`. Deux sources :

- la table `[datalog.devices.<id>]` de `habctrl.toml` : attributs d'équipements enregistrés à
  chaque changement, sous `devices/<id>/<attribut>` (le « . » devient « / » : `zone.Global` →
  `devices/fp2/zone/Global`) ;
- `record(chemin, valeur)` depuis une automatisation, pour tout le reste :

```python
from core.datalog import record

record("deshum/dewPoint", 12.4)                  # horodaté maintenant
record("deshum/dewPoint", 12.4, at=event.time)   # horodaté à l'événement
```

Réglages (`habctrl.toml`) :

```toml
[datalog]
url = "https://<base>.firebaseio.com"
credentials = "../HabSecrets/firebase.json"   # clé du compte de service, relative à HabConfig
retention = "30d"                              # par défaut ; unités h, d, y

[datalog.devices.prise_salon]
on = {}                                        # à chaque changement
active_power = { hysteresis = 5, retention = "1y" }

[datalog.purge]                                # rétention des chemins de record()
"deshum/dewPoint" = "18d"
```

- L'hystérésis ne s'applique qu'aux nombres : une valeur n'est enregistrée que si elle s'écarte
  d'au moins `hysteresis` de la dernière enregistrée.
- Un équipement injoignable n'enregistre rien.
- Envoi immédiat en tâche de fond ; sans réseau, la valeur est perdue (journalisé).
- Une fois par jour, les valeurs plus vieilles que leur rétention sont supprimées.
- Datalog non configuré : pas d'historique, les graphiques signalent l'erreur.

## Graphiques

`dashboard/charts.toml` décrit les graphiques de la page `/graphiques` :

```toml
[[charts]]
title = "Prise salon"
series = [
  { path = "devices/prise_salon/active_power", label = "Puissance", unit = "W" },
  { path = "devices/prise_salon/on", label = "Allumée", type = "bool" },
]
```

`path` est un chemin du datalog. `type` : `number` (par défaut, courbe) ou `bool` (bande
allumé / éteint sous la courbe). Les séries de même `unit` partagent un axe. Fichier absent :
aucun graphique.

## Appairage : déroulé et fichiers

Déroulé et `pairing/zigbee.toml` : voir `README.md`.

- **HomeKit** : la découverte mDNS écoute pendant la durée choisie ; les accessoires déjà
  appairés sont ignorés (les réinitialiser pour les appairer). L'accessoire est reconnu par son
  identifiant HomeKit (ex. après une réinitialisation). Le code HomeKit (XXX-XX-XXX) est saisi
  masqué. L'appairage est enregistré dans `pairing/homekit/<id>.toml` (droits 600) : en tête, ce
  que l'accessoire annonce (fabricant, modèle, n° de série, firmware) ; dans `[hap]`, les clés
  d'appairage d'aiohomekit, à ne pas modifier. Il est portable : l'adresse IP n'y figure jamais,
  elle est résolue par mDNS à chaque connexion. Copié sur une nouvelle installation, il reste
  valable tant que l'accessoire n'a pas été réinitialisé.
- **Zigbee** : le réseau est ouvert pendant la durée choisie : mettre l'équipement en mode
  appairage (souvent un appui long). Le premier équipement qui termine son interview est
  retenu ; il est reconnu par son IEEE. Le réseau (clés, canal) est mémorisé dans la clé USB.
  La base zigpy (équipements, interview, sauvegardes du réseau pour le reconstruire sur une
  nouvelle clé) est de la configuration : zigpy l'utilise dans `temp/`, une copie cohérente est
  gardée dans `pairing/zigbee.db`, écrite après chaque appairage et reprise au démarrage si
  `temp/` est vide. Changer de clé avec une copie ancienne peut obliger à réappairer les
  équipements : le compteur de trames du réseau, sauvegardé avec, a avancé depuis.

## États et commandes par type d'équipement

Commun à tous : `connected` (bool, lecture seule). Quand il vaut `False`, les autres attributs
disparaissent de l'état et se lisent `None`.

Dans une automatisation :

```python
salon.on = True                              # commande en tâche de fond, ne bloque pas
ok = await salon.send("brightness", 50, 2000)  # attend la réponse ; transition en ms
if prise_salon.on: ...                       # lecture de l'état mémorisé
```

Les équipements Zigbee et HomeKit se déclarent sans adresse : le lien avec l'appareil physique
est enregistré dans `pairing/` par l'appairage, qui crée aussi la déclaration.

### Ampoules — `yeelight.bulb`, `zigbee.light`

```python
salon = YeelightBulb(host="192.168.1.110")   # port=55443, transition=300
plafonnier = Light()                         # Zigbee ; transition=300
```

Interface commune (`core/light.py`) : une automatisation écrite pour une ampoule marche avec
l'autre.

| Attribut     | Type                      | Lecture | Commande                    |
|--------------|---------------------------|---------|-----------------------------|
| `on`         | bool                      | ✓       | `True`, `False`, `"toggle"` |
| `brightness` | int, 1 à 100 (%)          | ✓       | ✓                           |
| `color_temp` | int, Kelvin (~1700–6500)  | ✓       | ✓                           |
| `rgb`        | str, `"#rrggbb"`          | ✓       | ✓                           |

Commandes groupées, en tâche de fond comme `salon.on = True` :

| Commande                                   | Effet                                         |
|--------------------------------------------|-----------------------------------------------|
| `toggle(transition=None)`                  | inverse allumé / éteint                       |
| `set_rgb(brightness, rgb, transition=None)` | allume en couleur                            |
| `set_temp(brightness, color_temp, transition=None)` | allume en blanc                      |

Règles :
- régler `brightness`, `color_temp` ou `rgb` allume l'ampoule éteinte ;
- `color_temp` et `rgb` n'existent que si l'ampoule les a : sinon lecture `None`, écriture
  refusée (journalisée, rien d'autre ne s'arrête). `set_rgb` sur une ampoule blanche règle
  l'intensité puis s'arrête sur la couleur.

Transition : celle passée à la commande, sinon `transition=` de la déclaration
(300 ms par défaut) ; 0 = immédiat.

Yeelight : `set_rgb` et `set_temp` partent en un seul message. Zigbee : deux commandes
(intensité puis réglage) ; les réglages de couleur sont ceux que l'ampoule déclare à
l'appairage (`color_capabilities` ; non lu : tout est supposé présent) ; le `rgb` relu est
approché, à pleine intensité (l'ampoule donne sa couleur en coordonnées xy) ; transition au
dixième de seconde près.

### Prise Zigbee — `zigbee.plug`

```python
prise_salon = Plug()
```

| Attribut       | Type             | Lecture | Commande                    |
|----------------|------------------|---------|-----------------------------|
| `on`           | bool             | ✓       | `True`, `False`, `"toggle"` |
| `active_power` | float, W         | ✓       | —                           |
| `voltage`      | float, V         | ✓       | —                           |
| `current`      | float, A         | ✓       | —                           |
| `energy`       | float, kWh cumul | ✓       | —                           |

Les mesures sont envoyées spontanément par la prise, et n'apparaissent que si le modèle les
fournit (clusters 0x0b04, 0x0702). Une lecture toutes les 5 min vérifie seulement que la prise
répond encore.

### Capteurs Zigbee sur pile — `zigbee.climate`, `zigbee.motion`, `zigbee.contact`

```python
meteo_salon = Climate()
presence_entree = Motion()
porte_entree = Contact()
```

| Pilote    | Attribut      | Type                    |
|-----------|---------------|-------------------------|
| `climate` | `temperature` | float, °C               |
|           | `humidity`    | float, %                |
|           | `pressure`    | float, hPa              |
| `motion`  | `presence`    | bool                    |
|           | `illuminance` | int, lux                |
| `contact` | `open`        | bool                    |
| tous      | `battery`     | int, %                  |

Lecture seule, n'apparaissent que si l'appareil les envoie. L'appareil n'est jamais
interrogé (il dort) : joignable dès son premier message ; après un redémarrage, une valeur
reste absente (None) tant qu'il ne l'a pas envoyée. Un détecteur qui n'envoie jamais « plus
personne » (Xiaomi) est remis à faux par son correctif zha-quirks après un délai.

### Bouton Zigbee — `zigbee.button`

```python
bouton_lit = Button()
```

Seulement `battery` dans l'état. Chaque appui est un événement, nommé par le correctif
zha-quirks de l'appareil, avec sa valeur éventuelle :

| Appareil       | Événements                                          |
|----------------|-----------------------------------------------------|
| Xiaomi b1acn01 | `single`, `double`, `hold`, `release`               |
| Xiaomi rond    | `click` (valeur : type d'appui)                     |
| cube           | `flip`, `rotate_right`… (valeur : détails, dont l'angle) |
| Hue Dimmer     | `on_press`, `off_long_press`…                       |

```python
@on_event("bouton_lit.double")       # à chaque double appui ; @on_event("bouton_lit") : tous
async def lumiere(event):            # event.attr : nom, event.new : valeur (ex. angle du cube)
    salon.on = "toggle"
```

### Capteur de présence Aqara FP2 — `homekit.fp2`

```python
fp2 = FP2(zonemapping={0: "Global", 1: "salon"})
```

| Attribut                          | Type            | Lecture | Commande |
|-----------------------------------|-----------------|---------|----------|
| `zone.<alias>` ou `zone[<index>]` | bool (présence) | ✓       | —        |
| `illuminance`                     | float, lux      | ✓       | —        |

Chaque zone est repérée par son « Sensor Index » (caractéristique Aqara), comme dans
node-red-contrib-fp2 ; l'index 0 est la présence globale. `zonemapping` donne un alias aux
index : `fp2.zone.salon` ou `fp2.zone[1]` (l'index est traduit en alias s'il en a un) ; sans
alias : `fp2.zone[3]`. Déclencheur : `@on_change("fp2.zone.<alias>")` ; l'index n'est
utilisable que pour une zone sans alias (`@on_change("fp2.zone.3")`), car l'état ne contient
qu'une clé par zone. Capteur : toute commande est refusée.

### TV Samsung d'avant 2016 — `samsung.tv`

Ex. UE55ES7000, système Orsay, protocole « legacy ».

```python
tv = SamsungTV(host="192.168.1.104")   # port=55000, retry=30
```

| Attribut | Type | Lecture | Commande                        |
|----------|------|---------|---------------------------------|
| `on`     | bool | ✓       | `False` (extinction) seulement |

`connected` et `on` reflètent la connexion TCP permanente à la TV : éteinte, elle la coupe
(sinon le keepalive TCP le détecte) ; une nouvelle tentative a lieu toutes les `retry`
secondes. L'allumer par le réseau est impossible sur ces modèles. À la première connexion,
accepter « HabController » sur l'écran de la TV : elle s'en souvient.
