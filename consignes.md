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
- Son état est porté par de vrais attributs : `cuisine.on` lit la valeur
  gardée dans l'objet, sans recherche par nom ni reconstruction.
- Attributs = état, en lecture seule : un attribut d'état est déclaré par
  `on = State[bool]()` ; `cuisine.on = True` est refusé par Python.
- Seul le pilote l'écrit, à la réception : `Light.on.update(self, valeur)`.
  C'est l'annonce officielle du changement : elle déclenche `event_on` et met à
  jour les pages web. Aucune autre écriture n'est surveillée.
- Méthodes = actions sur l'objet : `cuisine.switch(True)`. Sans `await` : la
  commande part en tâche de fond, dans l'ordre, et le script n'attend pas.
- Paramètre non pris en charge par l'équipement (ex. transition) : ignoré.
- Une seule déclaration par attribut : `Device` connaît les attributs d'état
  de la classe et de ses parents ; l'état des pages, les valeurs suivies et la
  remise à zéro en découlent, rien n'est réécrit dans chaque classe.
- Équipement injoignable : ses attributs valent `None` (sauf les capacités).
- Si l'équipement ou son attribut n'existe pas, c'est Python qui le signale.

## Hiérarchie
- `Device` : connected.
- `TypeSwitch(Device)` : on, switch, toggle. Pour la lampe, la
  prise, le ruban, l'afficheur et la TV.
- `TypeLight(TypeSwitch)` : brightness, color_temp, rgb, rgb_capable.
- Capteurs Zigbee (`Sensor(Device)`, sur pile, battery) : `Climate`, `Contact`, `Motion`,
  `Button`. FP2 : illuminance et zones. Aspirateur : last_clean ; clean, pause,
  home, refresh.

## Capacités
- Une classe générique par catégorie. Ce qui dépend du modèle est une capacité
  (`rgb_capable = Capability[bool]()`, `battery_powered`), renseignée par le
  pilote, à l'appairage si possible, gardée hors connexion.
- Une action non prise en charge écrit un avertissement dans le journal, par
  exemple « salon_light ne prend pas en charge RGB ».

## Pages web
- Chaque widget est lié à sa catégorie d'équipement : pas de widget générique
  qui relit la structure pour reconstruire le HTML.
- Le passage entre Python et JavaScript (état en JSON, méthode appelée par
  son nom) est la seule entorse acceptée au typage fort.

## Générique accepté
- Détection des fonctions d'événement dans les automatisations.
- Noms d'attributs dans le paramétrage (datalog.toml, reports).
- État de surveillance des valeurs (tracked, disabled, absent).
