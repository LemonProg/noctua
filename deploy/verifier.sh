#!/usr/bin/env bash
# Vérification de l'installation, à lancer sur le VPS après installation :
#   sudo bash /srv/agence-ia/app/deploy/verifier.sh
#
# Travaille sur une base et un dossier d'essai (supprimés à la fin), avec le vrai code,
# le vrai utilisateur et les vrais dossiers : la base de production n'est pas touchée.
set -uo pipefail

APP="${APP:-/srv/agence-ia/app}"
UTILISATEUR="${VERIF_UTILISATEUR:-agence}"
ESSAI="${VERIF_DOSSIER:-/srv/agence-ia/essai-verification}"
TRAVAIL="${VERIF_TRAVAIL:-/var/tmp/agence-ia/essai-verification}"
ECHECS=0

export DOSSIER_CONTENUS="$ESSAI/contenus" DOSSIER_TEMP="$TRAVAIL" CHEMIN_BASE="$ESSAI/essai.sqlite3"

en_agence() {
    if [[ "$UTILISATEUR" == "$(id -un)" ]]; then
        "$@"
    else
        sudo -u "$UTILISATEUR" -H env DOSSIER_CONTENUS="$DOSSIER_CONTENUS" DOSSIER_TEMP="$DOSSIER_TEMP" \
            CHEMIN_BASE="$CHEMIN_BASE" PATH="/usr/local/bin:/usr/bin:/bin" "$@"
    fi
}
agence() { (cd "$APP" && en_agence "$APP/.venv/bin/agence" "$@"); }
python() { (cd "$APP" && en_agence "$APP/.venv/bin/python" "$@"); }
verifier() {
    local libelle="$1"; shift
    if "$@"; then echo "  OK     $libelle"; else echo "  ÉCHEC  $libelle"; ECHECS=$((ECHECS + 1)); fi
}
statut_tache() { agence tache lister | awk 'NR > 1 && $1 == "1" {print $4}'; }

nettoyer() {
    [[ -n "${PID_WORKER:-}" ]] && kill "$PID_WORKER" 2>/dev/null
    rm -rf "$ESSAI" "$TRAVAIL"
}
trap nettoyer EXIT

echo "==> Préparation de l'essai dans $ESSAI"
rm -rf "$ESSAI" "$TRAVAIL"
en_agence mkdir -p "$ESSAI"
python -c "import sys; from PIL import Image; Image.new('RGB', (64, 48), (113, 60, 245)).save(sys.argv[1])" "$ESSAI/photo-essai.jpg"
agence init >/dev/null

echo "==> 1. Ajout d'un rush"
verifier "rush ajouté" agence rush ajouter "$ESSAI/photo-essai.jpg"
verifier "rush visible dans la liste" grep -q photo-essai.jpg <(agence rush lister)

echo "==> 2. Circuit d'une tâche avec le worker factice"
agence tache creer brief_texte --json '{"objectif":"Essai","format":"post","reseau":"instagram","date_prevue":"2026-11-05T18:00:00+01:00"}' >/dev/null
verifier "tâche créée « à_faire »" test "$(statut_tache)" = "à_faire"
(cd "$APP" && en_agence "$APP/.venv/bin/python" -m noyau.worker --agent-factice >"$ESSAI/../essai-worker.log" 2>&1) &
PID_WORKER=$!
for _ in $(seq 1 50); do [[ "$(statut_tache)" == "à_valider" ]] && break; sleep 0.2; done
verifier "tâche passée « à_valider » par le worker" test "$(statut_tache)" = "à_valider"
pkill -TERM -f "noyau.worker --agent-factice" 2>/dev/null
wait "$PID_WORKER" 2>/dev/null
PID_WORKER=""
verifier "worker arrêté proprement" grep -q "Arrêt demandé" "$ESSAI/../essai-worker.log"
rm -f "$ESSAI/../essai-worker.log"

echo "==> 3. Corbeille : suppression après 24 h (date simulée)"
agence rush ecarter 1 >/dev/null
FICHIER="$(python -c "
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute('SELECT chemin FROM assets WHERE id = 1').fetchone()[0])" "$CHEMIN_BASE")"
verifier "rush en corbeille, fichier encore présent" test -f "$DOSSIER_CONTENUS/$FICHIER"
python -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE assets SET corbeille_le = '2000-01-01T00:00:00+00:00'\"); c.commit()" "$CHEMIN_BASE"
agence purge >/dev/null
verifier "fichier supprimé définitivement" test ! -e "$DOSSIER_CONTENUS/$FICHIER"

echo "==> 4. Aucun fichier de travail restant"
verifier "dossier de travail vide" test -z "$(ls -A "$TRAVAIL" 2>/dev/null)"

echo
if [[ $ECHECS -eq 0 ]]; then
    echo "Vérification de l'installation : tout est OK."
else
    echo "Vérification de l'installation : $ECHECS échec(s)."
    exit 1
fi
