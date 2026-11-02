# Déploiement sur le VPS

VPS visé : Ubuntu 24.04 ou Debian 12, accès SSH sur le port 22 avec un utilisateur qui peut faire `sudo` (ou root).

## Installer ou mettre à jour

Depuis votre ordinateur, à la racine du projet :

```bash
deploy/envoyer.sh utilisateur@adresse-du-vps [domaine]
```

Avec un domaine (ex. `agence.exemple.fr`, qui doit pointer vers le VPS), l'interface est servie en HTTPS par Caddy.

Le script envoie le code avec `rsync` (jamais `.env`, la base, les contenus ni `references/`), puis lance
`deploy/installer.sh` sur le VPS. L'installation peut être relancée sans risque à chaque mise à jour. Elle :

- installe les paquets système (`rsync`, `ufw`, `curl`) ;
- crée un swap de 2 Go (`/swapfile`) s'il n'y en a pas, utilisé seulement en dernier recours ;
- active le pare-feu : seul le port SSH 22 est ouvert ;
- crée l'utilisateur `agence` et les dossiers `/srv/agence-ia/{app,contenus}` et `/var/tmp/agence-ia` ;
- installe `uv`, Python 3.12 et les dépendances dans `/srv/agence-ia/app/.venv` ;
- crée `/srv/agence-ia/app/.env` depuis `.env.example` s'il n'existe pas (à relire) ;
- installe le navigateur du rendu des visuels (Chromium sur Debian 12, Google Chrome par son dépôt officiel sur Ubuntu 24.04) et Claude Code pour l'utilisateur `agence` ;
- installe la purge horaire (`agence-purge.timer`) et les services du worker et de l'interface, sans les activer ;
- avec un domaine : installe Caddy et ouvre les ports 80 et 443.

Après la première installation, compléter `/srv/agence-ia/app/.env` (`sudo -u agence nano /srv/agence-ia/app/.env`) :
- `CLAUDE_CODE_OAUTH_TOKEN` : jeton généré sur votre ordinateur avec `claude setup-token` (abonnement Claude Pro ou Max) ;
- `MOT_DE_PASSE_INTERFACE` et `SECRET_SESSION` : avec `sudo agence mot-de-passe` ;
- `HOTE_WEB=127.0.0.1` (derrière Caddy).

Puis : `sudo systemctl enable --now agence-worker agence-web`.

Les polices de la marque (`app/polices-marque/`, non versionnées) et `charte.md` sont envoyées par rsync avec le code.

À vérifier lors de la première installation : le rendu Chromium sous systemd (bac à sable du navigateur avec `NoNewPrivileges`) ; si les visuels échouent, consulter `journalctl -u agence-worker`.

## Vérifier l'installation

Sur le VPS :

```bash
sudo bash /srv/agence-ia/app/deploy/verifier.sh
```

Il travaille sur une base d'essai supprimée à la fin : ajout d'un rush, circuit d'une tâche avec le worker
factice, suppression après 24 h (date simulée), dossier de travail vide.

## Banc de test du rendu vidéo

À lancer une fois l'installation terminée, quand rien d'autre ne tourne (le premier rendu télécharge le Chrome
d'HyperFrames) :

```bash
sudo agence video banc --durees 15 30 60
```

Il rend trois reels synthétiques avec les réglages de production et affiche, pour chacun, le temps de rendu et
la mémoire maximale (Node, Chrome et FFmpeg compris). Ces mesures fixent la durée maximale des reels, le nombre de workers et le délai du rendu. Repères sur un ordinateur de
développement (8 cœurs, 16 Go) : 1,3 s de rendu par seconde de vidéo, environ 1,2 Go de mémoire au plus fort.

## Banque de bruitages

Une fois, après l'installation (relançable à tout moment : elle complète seulement ce qui manque) :

```bash
sudo agence sons importer
```

Elle cherche sur Openverse environ 75 vrais bruitages (15 catégories × 5), **CC0 uniquement**, revérifie la
licence sur la page Freesound de chaque son et les range dans le stockage (moins de 30 Mo). Compter quelques
minutes : l'API d'Openverse est parfois lente ou indisponible ; dans ce cas, relancer plus tard. Ensuite,
écouter les sons dans l'interface (Réglages > Sons) et écarter ceux qui ne conviennent pas, puis relancer
l'import pour compléter. Tant que la banque est vide, les reels utilisent des sons synthétisés (Web Audio).

## Sauvegardes de la base

Chaque nuit vers 4 h 45, une copie compressée de la base (posts, leçons, direction artistique, statistiques)
est faite dans `/srv/agence-ia/sauvegardes` ; les 7 plus récentes sont gardées (`SAUVEGARDES_CONSERVEES`).
Ni les médias ni `.env` n'y sont. Pour en garder une copie sur votre ordinateur :

```bash
deploy/recuperer-sauvegardes.sh utilisateur@adresse-du-vps
```

Restaurer une sauvegarde :

```bash
sudo systemctl stop agence-worker agence-web
sudo -u agence bash -c 'gunzip -c /srv/agence-ia/sauvegardes/agence-AAAAMMJJ-HHMMSS.sqlite3.gz > /srv/agence-ia/agence.sqlite3'
sudo rm -f /srv/agence-ia/agence.sqlite3-wal /srv/agence-ia/agence.sqlite3-shm
sudo systemctl start agence-worker agence-web
```

## Commandes utiles sur le VPS

```bash
sudo agence disque                     # espace disque et alertes
sudo agence sauvegarde                 # sauvegarde immédiate de la base
sudo agence notification sujet         # crée le sujet ntfy secret des notifications push
sudo agence notification test          # envoie une notification de test
sudo agence rush ajouter /tmp/photo.jpg  # le fichier doit être lisible par l'utilisateur agence (ex. dans /tmp)
sudo agence rush lister
sudo agence corbeille lister
sudo agence tache lister
systemctl list-timers agence-purge.timer   # prochaine purge
journalctl -u agence-purge.service -n 50   # dernières purges
sudo -u agence nano /srv/agence-ia/app/.env  # modifier la configuration
```

Pour envoyer une photo depuis votre ordinateur : `scp photo.jpg utilisateur@adresse-du-vps:/tmp/`.
