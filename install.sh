#!/bin/bash
# Installation idempotent
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

python3 -m venv "$REPO_DIR/.venv"
source "$REPO_DIR/.venv/bin/activate"
pip install -r "$REPO_DIR/requirements.txt"
