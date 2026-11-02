#!/usr/bin/env bash
# Installation et mise à jour de l'agence sur le VPS (Ubuntu 24.04 ou Debian 12).
# Lancé par deploy/envoyer.sh, en root. Idempotent : peut être relancé à chaque mise à jour.
#
# Usage : sudo [DOMAINE=agence.exemple.fr] bash installer.sh /chemin/du/code/envoye
# Avec DOMAINE, l'interface est servie en HTTPS par Caddy sur ce nom (ports 80 et 443 ouverts).
set -euo pipefail

SOURCE="${1:?Usage : sudo bash installer.sh /chemin/du/code/envoye}"
UTILISATEUR="agence"
RACINE="/srv/agence-ia"
APP="$RACINE/app"
CONTENUS="$RACINE/contenus"
TRAVAIL="/var/tmp/agence-ia"
SWAP_TAILLE="2G"
PORT_SSH=22
UV_VERSION="0.10.9"
# Node pour le rendu des reels (HyperFrames exige Node 22 ou plus) : archive officielle figée, vérifiée par empreinte.
NODE_VERSION="22.23.3"
declare -A NODE_SHA256=(
    [x64]="df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de"
    [arm64]="a44aeb94849a299b22df10b9e622ec2f605c2183501bc40590705131de7c740f"
)
DOMAINE="${DOMAINE:-}"

etape() { echo; echo "==> $*"; }
en_agence() { sudo -u "$UTILISATEUR" -H env PATH="/usr/local/bin:/usr/bin:/bin" "$@"; }

[[ $EUID -eq 0 ]] || { echo "À lancer en root (sudo)." >&2; exit 1; }
[[ -f "$SOURCE/pyproject.toml" ]] || { echo "Code introuvable dans $SOURCE" >&2; exit 1; }

# shellcheck source=/dev/null
. /etc/os-release
case "$ID:$VERSION_ID" in
    ubuntu:24.04|debian:12) ;;
    *) echo "Système non prévu : $PRETTY_NAME (attendu : Ubuntu 24.04 ou Debian 12)." >&2; exit 1 ;;
esac

etape "Paquets système"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq rsync ufw curl ca-certificates sudo gnupg xz-utils ffmpeg >/dev/null

etape "Navigateur pour le rendu des visuels"
if command -v chromium >/dev/null || command -v google-chrome-stable >/dev/null; then
    echo "Navigateur déjà installé."
elif [[ "$ID" == "debian" ]]; then
    apt-get install -y -qq chromium >/dev/null
else
    # Ubuntu 24.04 : le paquet chromium est un snap, mal adapté à un service systemd ; on prend le dépôt officiel de Google.
    [[ "$(dpkg --print-architecture)" == "amd64" ]] || { echo "Google Chrome n'existe qu'en amd64 : installez un Chromium non snap." >&2; exit 1; }
    install -d -m 755 /etc/apt/keyrings
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor --yes -o /etc/apt/keyrings/google-chrome.gpg
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list
    apt-get update -qq && apt-get install -y -qq google-chrome-stable >/dev/null
fi

etape "Swap de $SWAP_TAILLE"
if swapon --show --noheadings | grep -q .; then
    echo "Swap déjà actif :"; swapon --show
else
    fallocate -l "$SWAP_TAILLE" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "Swap créé : /swapfile"
fi
# N'utiliser le swap qu'en dernier recours (la RAM reste prioritaire).
echo 'vm.swappiness=10' > /etc/sysctl.d/90-agence-ia.conf
sysctl -q -p /etc/sysctl.d/90-agence-ia.conf

etape "Pare-feu (SSH sur le port $PORT_SSH uniquement)"
ufw allow "$PORT_SSH/tcp" >/dev/null
if [[ -n "$DOMAINE" ]]; then ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; fi
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw --force enable >/dev/null
ufw status | sed -n '1,10p'

etape "Utilisateur dédié « $UTILISATEUR » et dossiers"
id "$UTILISATEUR" >/dev/null 2>&1 || useradd --system --home-dir "$RACINE" --shell /usr/sbin/nologin "$UTILISATEUR"
install -d -o "$UTILISATEUR" -g "$UTILISATEUR" -m 750 "$RACINE" "$APP" "$CONTENUS" "$TRAVAIL"

etape "Copie du code dans $APP"
rsync -a --delete \
    --exclude=".env" --exclude=".venv" --exclude="lecons.md" --exclude="direction_artistique.md" --exclude="*.sqlite3*" \
    --chown="$UTILISATEUR:$UTILISATEUR" \
    "$SOURCE/" "$APP/"

etape "uv $UV_VERSION et Python 3.12"
if ! command -v uv >/dev/null || [[ "$(uv --version | cut -d' ' -f2)" != "$UV_VERSION" ]]; then
    curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
cd "$APP"
[[ -x "$APP/.venv/bin/python" ]] || en_agence uv venv --quiet --python 3.12 "$APP/.venv"
en_agence uv pip install --quiet --python "$APP/.venv" -e .
en_agence "$APP/.venv/bin/python" --version

etape "Node $NODE_VERSION et HyperFrames (rendu des reels)"
case "$(dpkg --print-architecture)" in amd64) NODE_ARCH="x64" ;; arm64) NODE_ARCH="arm64" ;;
    *) echo "Architecture non prévue pour Node." >&2; exit 1 ;; esac
if [[ "$(node --version 2>/dev/null)" != "v$NODE_VERSION" ]]; then
    ARCHIVE="node-v$NODE_VERSION-linux-$NODE_ARCH.tar.xz"
    curl -fsSL -o "/tmp/$ARCHIVE" "https://nodejs.org/dist/v$NODE_VERSION/$ARCHIVE"
    echo "${NODE_SHA256[$NODE_ARCH]}  /tmp/$ARCHIVE" | sha256sum -c --quiet
    rm -rf /usr/local/lib/nodejs && mkdir -p /usr/local/lib/nodejs
    tar -xJf "/tmp/$ARCHIVE" -C /usr/local/lib/nodejs --strip-components=1 && rm -f "/tmp/$ARCHIVE"
    ln -sf /usr/local/lib/nodejs/bin/{node,npm,npx} /usr/local/bin/
fi
node --version
# Versions figées par video/package-lock.json ; HyperFrames télécharge son Chrome au premier rendu
# (dans ~/.cache/hyperframes de l'utilisateur agence). Ni télémétrie ni mise à jour automatique.
en_agence env HYPERFRAMES_NO_TELEMETRY=1 HYPERFRAMES_NO_UPDATE_CHECK=1 HYPERFRAMES_NO_OPTIONAL_INSTALL=1 \
    npm ci --prefix "$APP/video" --no-audit --no-fund --loglevel=error

etape "Claude Code (abonnement Claude de l'agence)"
if [[ ! -x "$RACINE/.local/bin/claude" ]]; then
    en_agence bash -c "curl -fsSL https://claude.ai/install.sh | bash" >/dev/null
fi
en_agence "$RACINE/.local/bin/claude" --version

etape "Configuration (.env)"
if [[ ! -f "$APP/.env" ]]; then
    install -o "$UTILISATEUR" -g "$UTILISATEUR" -m 600 "$APP/.env.example" "$APP/.env"
    echo "ATTENTION : $APP/.env créé depuis .env.example. Vérifiez-le : sudo -u $UTILISATEUR nano $APP/.env"
else
    echo ".env existant conservé."
fi
en_agence "$APP/.venv/bin/agence" init

# Raccourci : « sudo agence <commande> » lance la commande sous l'utilisateur dédié.
cat > /usr/local/bin/agence <<EOF
#!/bin/sh
cd "$APP" && exec sudo -u "$UTILISATEUR" -H "$APP/.venv/bin/agence" "\$@"
EOF
chmod 755 /usr/local/bin/agence

etape "Services systemd"
install -m 644 "$APP"/deploy/systemd/agence-*.service "$APP"/deploy/systemd/agence-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now agence-purge.timer agence-sauvegarde.timer >/dev/null
install -d -o "$UTILISATEUR" -g "$UTILISATEUR" -m 700 "$RACINE/sauvegardes"
for service in agence-worker agence-web; do
    if systemctl is-enabled --quiet "$service.service"; then
        systemctl restart "$service.service"
        echo "$service redémarré."
    else
        echo "$service installé mais non activé. Une fois .env rempli (jeton Claude, mot de passe) :"
        echo "  sudo systemctl enable --now $service"
    fi
done

if [[ -n "$DOMAINE" ]]; then
    etape "Caddy (HTTPS pour $DOMAINE)"
    apt-get install -y -qq caddy >/dev/null
    sed "s/DOMAINE/$DOMAINE/" "$APP/deploy/Caddyfile.exemple" > /etc/caddy/Caddyfile
    systemctl reload caddy || systemctl restart caddy
    echo "Interface : https://$DOMAINE (le nom doit pointer vers ce serveur)."
fi
systemctl list-timers agence-purge.timer agence-sauvegarde.timer --no-pager | sed -n '1,4p'

etape "Installation terminée"
df -h "$RACINE" | tail -1 | awk '{print "Disque : " $4 " libres sur " $2}'
free -h | awk '/^Mem|^Swap/ {print $1 " " $2 " (utilisé : " $3 ")"}'
