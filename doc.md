# États et commandes par type d'équipement

Commun à tous : `connected` (bool, lecture seule). Quand il vaut `False`, les autres attributs
disparaissent de l'état et se lisent `None`.

Dans une automatisation :

```python
salon.on = True                              # commande en tâche de fond, ne bloque pas
ok = await salon.send("brightness", 50, 2000)  # attend la réponse ; transition en ms
if prise_salon.on: ...                       # lecture de l'état mémorisé
```

## Ampoules — `yeelight.bulb`, `zigbee.light`

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
l'appairage (`color_capabilities`) ; le `rgb` relu est approché (l'ampoule donne sa couleur en
coordonnées xy) ; transition au dixième de seconde près.

## Prise Zigbee — `zigbee.plug`

| Attribut       | Type             | Lecture | Commande                    |
|----------------|------------------|---------|-----------------------------|
| `on`           | bool             | ✓       | `True`, `False`, `"toggle"` |
| `active_power` | float, W         | ✓       | —                           |
| `voltage`      | float, V         | ✓       | —                           |
| `current`      | float, A         | ✓       | —                           |
| `energy`       | float, kWh cumul | ✓       | —                           |

Les mesures sont envoyées spontanément par la prise, et n'apparaissent que si le modèle les
fournit. Une lecture toutes les 5 min vérifie seulement que la prise répond encore.

## Capteurs Zigbee sur pile — `zigbee.climate`, `zigbee.motion`, `zigbee.contact`

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
reste absente (None) tant qu'il ne l'a pas envoyée.

## Bouton Zigbee — `zigbee.button`

Seulement `battery` dans l'état. Chaque appui est un événement, nommé par le correctif
zha-quirks de l'appareil (`single`, `double`, `hold`, `release`, `click`, `flip`,
`rotate_right`, `on_press`…), avec sa valeur éventuelle :

```python
@on_event("bouton_lit.double")       # à chaque double appui ; @on_event("bouton_lit") : tous
async def lumiere(event):            # event.attr : nom, event.new : valeur (ex. angle du cube)
    salon.on = "toggle"
```

## Capteur de présence Aqara FP2 — `homekit.fp2`

| Attribut                          | Type            | Lecture | Commande |
|-----------------------------------|-----------------|---------|----------|
| `zone.<alias>` ou `zone[<index>]` | bool (présence) | ✓       | —        |
| `illuminance`                     | float, lux      | ✓       | —        |

Les zones dépendent de l'appareil et de `zonemapping` dans la déclaration ; l'index 0 est
la présence globale. Capteur : toute commande est refusée.

## TV Samsung d'avant 2016 — `samsung.tv`

| Attribut | Type | Lecture | Commande                        |
|----------|------|---------|---------------------------------|
| `on`     | bool | ✓       | `False` (extinction) seulement |

`connected` et `on` reflètent la connexion TCP permanente à la TV (port 55000) : éteinte, elle
la coupe. L'allumer par le réseau est impossible sur ces modèles. À la première connexion,
accepter « HabController » sur l'écran de la TV.
