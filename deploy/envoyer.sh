#!/usr/bin/env bash
# Envoie le code depuis votre ordinateur vers le VPS, puis lance l'installation (idempotente).
#
# Usage : deploy/envoyer.sh utilisateur@adresse-du-vps [domaine]
#   domaine (facultatif) : nom servi en HTTPS par Caddy, ex. agence.exemple.fr
#
# Le code est d'abord copié dans ~/agence-ia-envoi (dossier de l'utilisateur SSH, sans
# besoin de droits root), puis installer.sh le recopie dans /srv/agence-ia/app avec sudo.
# Ne sont jamais envoyés : .env, la base, les contenus, l'environnement Python, references/.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage : $0 utilisateur@adresse-du-vps [domaine]" >&2
    exit 1
fi
VPS="$1"
DOMAINE="${2:-}"
[[ "$DOMAINE" =~ ^[A-Za-z0-9.-]*$ ]] || { echo "Domaine invalide : $DOMAINE" >&2; exit 1; }
RACINE="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Envoi du code vers $VPS:~/agence-ia-envoi"
rsync -az --delete \
    --exclude=".git/" \
    --exclude=".venv" \
    --exclude="video/node_modules/" \
    --exclude="tests/image/" \
    --exclude="direction_artistique.md" \
    --exclude=".env" \
    --exclude="references/" \
    --exclude="donnees/" \
    --exclude="tmp/" \
    --exclude="lecons.md" \
    --exclude="*.sqlite3*" \
    --exclude="__pycache__/" \
    --exclude=".pytest_cache/" \
    --exclude="*.egg-info/" \
    --exclude=".coverage" \
    --exclude=".DS_Store" \
    "$RACINE/" "$VPS:agence-ia-envoi/"

echo "==> Installation sur le VPS (le mot de passe sudo peut être demandé)"
ssh -t "$VPS" "sudo DOMAINE=$DOMAINE bash \"\$HOME/agence-ia-envoi/deploy/installer.sh\" \"\$HOME/agence-ia-envoi\""
