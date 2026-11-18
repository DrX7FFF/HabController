# Consignes de conception

## Philosophie
- Python et des bibliothèques Python, rien d'autre.
- Un cœur qui s'occupe de tout : équipements, protocoles, datalog, pages web.
- L'utilisateur n'ajoute que du paramétrage (HabConfig) et du code Python pour
  les automatisations.
- Le programme le plus simple possible : Python fait déjà tout, on s'en sert
  au lieu de le réinventer. Pas de couche maison au-dessus du langage.

## Équipements
- Un équipement est un objet Python ordinaire : classes et héritage.
- Son état est porté par de vrais attributs : `cuisine.on` lit l'attribut,
  sans recherche par nom ni reconstruction.
- Le pilote met à jour l'attribut à la réception : `self.on = ...`.
- Une affectation d'attribut public (nom sans `_`) qui change sa valeur
  déclenche les automatisations et la mise à jour des pages web. Un seul
  mécanisme, dans `Device`.
- Les actions sont des méthodes : `await cuisine.turn_on()`.
- Si l'équipement ou son attribut n'existe pas, c'est Python qui le signale.

## Hiérarchie
- `Device` : connected, battery_powered, battery.
- `Switchable(Device)` : on, turn_on, turn_off, toggle. Pour la lampe, la
  prise, le ruban, l'afficheur et la TV.
- `Light(Switchable)` : brightness, color_temp, rgb, rgb_capable.
- Capteurs, présence et aspirateur : à définir.

## Capacités
- Une classe générique par catégorie. Ce qui dépend du modèle est un attribut
  (`rgb_capable`, `battery_powered`), renseigné par le pilote, à l'appairage
  si possible.
- Une action non prise en charge écrit un avertissement dans le journal, par
  exemple « salon_light ne prend pas en charge RGB ».

## Pages web
- Chaque widget est lié à sa catégorie d'équipement : pas de widget générique
  qui relit la structure pour reconstruire le HTML.

## Générique accepté
- Détection des fonctions d'événement dans les automatisations.
- Noms d'attributs dans le paramétrage (datalog.toml).
