# Récupération du token du Roborock S5 Max (Xiaomi Home)

Outil : Xiaomi Cloud Tokens Extractor (https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)

## Prérequis

- Robot appairé dans **Xiaomi Home**.
- Identifiants du compte **Xiaomi Home** (pas ceux de l'appli Roborock).
- IP du robot fixe (réservation DHCP), sinon l'IP enregistrée dans HabController deviendra fausse.

## 1. Installer l'outil

```bash
wget https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor/releases/latest/download/token_extractor.zip
unzip token_extractor.zip
cd token_extractor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 2. Lancer l'extraction

```bash
.venv/bin/python token_extractor.py
```

1. Choisir la méthode de connexion :
   - identifiant (e-mail ou ID de compte Xiaomi) + mot de passe ;
   - ou QR code, à scanner avec l'appli Xiaomi Home.
2. Si une validation 2FA est demandée : ouvrir le lien reçu **sur la machine qui exécute l'outil**.
3. Si un captcha est demandé : saisir le texte de l'image indiquée.
4. Région du serveur : `de` (Europe), ou laisser vide pour interroger toutes les régions.

## 3. Relever les informations

L'outil liste tous les appareils du compte. Pour le robot, noter :

- **IP**
- **TOKEN** (32 caractères hexadécimaux)

## 4. Vérifier en local

Depuis le serveur HabController, avec python-miio installé :

```bash
miiocli roborockvacuum --ip <IP> --token <TOKEN> status
```

Une réponse avec l'état et la batterie confirme que le token est valide et que le robot est joignable (UDP 54321).

## 5. Renseigner HabController

Reporter `host` et `token` dans le fichier d'équipement du robot dans `devices/`.

## Quand recommencer

Le token change si le Wi-Fi du robot est réinitialisé ou si le robot est réappairé dans Xiaomi Home.

## En cas d'échec de connexion

- Vérifier qu'il s'agit bien des identifiants Xiaomi Home.
- Désactiver temporairement les bloqueurs DNS du réseau (AdGuard, Pi-hole…) et un DNS Cloudflare.
- Essayer la connexion par QR code.
