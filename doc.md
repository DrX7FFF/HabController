# États et commandes par type d'équipement

Commun à tous : `connected` (bool, lecture seule). Quand il vaut `False`, les autres attributs
disparaissent de l'état et se lisent `None`.

Dans une automatisation :

```python
salon.on = True                              # commande en tâche de fond, ne bloque pas
ok = await salon.send("brightness", 50, 2000)  # attend la réponse ; transition en ms
if prise_salon.on: ...                       # lecture de l'état mémorisé
```

## Ampoule Yeelight — `yeelight.bulb`

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
| `set_rgb(brightness, rgb, transition=None)` | allume en couleur, en un seul message        |
| `set_temp(brightness, color_temp, transition=None)` | allume en blanc, en un seul message  |

Transition : celle passée à la commande, sinon `transition=` de la déclaration
(300 ms par défaut) ; 0 = immédiat.

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

## Capteur de présence Aqara FP2 — `homekit.fp2`

| Attribut                          | Type            | Lecture | Commande |
|-----------------------------------|-----------------|---------|----------|
| `zone.<alias>` ou `zone[<index>]` | bool (présence) | ✓       | —        |
| `illuminance`                     | float, lux      | ✓       | —        |

Les zones dépendent de l'appareil et de `zonemapping` dans la déclaration ; l'index 0 est
la présence globale. Capteur : toute commande est refusée.
