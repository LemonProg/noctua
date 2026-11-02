#!/usr/bin/env bash
# Rapatrie sur votre ordinateur les sauvegardes de la base faites chaque nuit sur le VPS (une copie hors du serveur).
#
# Usage : deploy/recuperer-sauvegardes.sh utilisateur@adresse-du-vps [dossier-local]
#   dossier-local (facultatif) : par défaut ~/agence-ia-donnees/sauvegardes-vps (hors iCloud)
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage : $0 utilisateur@adresse-du-vps [dossier-local]" >&2
    exit 1
fi
VPS="$1"
LOCAL="${2:-$HOME/agence-ia-donnees/sauvegardes-vps}"
mkdir -p "$LOCAL"
# Les sauvegardes appartiennent à l'utilisateur agence : lecture via sudo sur le VPS.
rsync -av --rsync-path="sudo rsync" "$VPS:/srv/agence-ia/sauvegardes/" "$LOCAL/"
echo "Sauvegardes dans $LOCAL"
