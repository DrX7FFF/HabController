from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# calculé depuis l'emplacement du code : indépendant du dossier de lancement
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"  # données locales (appairages...) : hors dépôt, voir .gitignore
