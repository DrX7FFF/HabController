# Récupération du token du Roborock S5 Max via Xiaomi Home

Xiaomi Cloud Tokens Extractor (https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)

## Installation

```bash
wget https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor/releases/latest/download/token_extractor.zip
unzip token_extractor.zip
cd token_extractor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Execution

```bash
.venv/bin/python token_extractor.py
```

1. Choisir la méthode de connexion (Q) et utiliser le lien proposé :
   - identifiant (e-mail ou ID de compte Xiaomi) + mot de passe ;
   - ou QR code, à scanner avec l'appli Xiaomi Home.
2. Région du serveur : laisser vide pour interroger toutes les régions.

## Vérification

Depuis le serveur HabController, avec python-miio installé :

```bash
.venv/bin/miiocli roborockvacuum --ip <IP> --token <TOKEN> status
```
