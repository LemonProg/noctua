# Deploying to a VPS

Target: Ubuntu 24.04 or Debian 12, SSH on port 22 with a user that can `sudo` (or root).
Recommended: 2 cores, 4 GB RAM, 40 GB disk.

## Install or update

From your computer, at the project root (with `charte.md` filled in):

```bash
deploy/envoyer.sh user@your-vps [domain]
```

With a domain (e.g. `agency.example.com`, pointing to the VPS), the interface is served over HTTPS by Caddy.

The script sends the code with `rsync` (never `.env`, the database, the content or `references/`), then runs
`deploy/installer.sh` on the VPS. The installation is idempotent and can be re-run on every update. It:

- installs system packages (`rsync`, `ufw`, `curl`, `ffmpeg`…);
- creates a 2 GB swap file (`/swapfile`) if there is none, used only as a last resort;
- enables the firewall: only SSH (22) is open;
- creates the `agence` user and the `/srv/agence-ia/{app,contenus}` and `/var/tmp/agence-ia` folders;
- installs `uv`, Python 3.12 and the dependencies in `/srv/agence-ia/app/.venv`;
- installs Node.js 22 and the video rendering dependencies;
- creates `/srv/agence-ia/app/.env` from `.env.example` if it does not exist (review it);
- installs the browser used to render visuals (Chromium on Debian 12, Google Chrome from its official repository on Ubuntu 24.04) and Claude Code for the `agence` user;
- installs the hourly purge (`agence-purge.timer`), the nightly backup and the worker and web services, without enabling them;
- with a domain: installs Caddy and opens ports 80 and 443.

After the first installation, complete `/srv/agence-ia/app/.env` (`sudo -u agence nano /srv/agence-ia/app/.env`):

- `CLAUDE_CODE_OAUTH_TOKEN`: token generated on your computer with `claude setup-token` (Claude Pro or Max subscription);
- `MOT_DE_PASSE_INTERFACE` and `SECRET_SESSION`: with `sudo agence mot-de-passe`;
- `HOTE_WEB=127.0.0.1` (behind Caddy).

Then: `sudo systemctl enable --now agence-worker agence-web`.

Brand fonts (`app/polices-marque/`, not versioned) and `charte.md` are sent by rsync along with the code.

If visuals fail to render under systemd (browser sandbox with `NoNewPrivileges`), check `journalctl -u agence-worker`.

## Check the installation

On the VPS:

```bash
sudo bash /srv/agence-ia/app/deploy/verifier.sh
```

It works on a throwaway database deleted at the end: adds a footage file, runs a task through the worker with a
fake agent, deletes it after 24 h (simulated clock) and checks the work folder is empty.

## Video rendering benchmark

Run once after installation, when nothing else is running (the first render downloads HyperFrames' Chrome):

```bash
sudo agence video banc --durees 15 30 60
```

It renders three synthetic reels with the production settings and shows, for each, the render time and the peak
memory (Node, Chrome and FFmpeg included). These figures set the maximum reel length, the number of workers and the
render timeout. Reference on a development machine (8 cores, 16 GB): 1.3 s of rendering per second of video,
about 1.2 GB of memory at peak.

## Sound effects library

Once, after installation (can be re-run at any time: it only fills what is missing):

```bash
sudo agence sons importer
```

It searches Openverse for about 75 real sound effects (15 categories × 5), **CC0 only**, re-checks the licence on
each sound's Freesound page and stores them (under 30 MB). Allow a few minutes: the Openverse API is sometimes slow
or down; if so, try again later. Then listen to the sounds in the interface (Réglages > Sons), discard those you
don't like and re-run the import to fill the gaps. While the library is empty, reels use sounds synthesised with
Web Audio.

## Database backups

Every night around 4:45 am, a compressed copy of the database (posts, lessons, art direction, statistics) is saved
to `/srv/agence-ia/sauvegardes`; the 7 most recent are kept (`SAUVEGARDES_CONSERVEES`). Neither media nor `.env`
are included. To keep a copy on your computer:

```bash
deploy/recuperer-sauvegardes.sh user@your-vps
```

Restore a backup:

```bash
sudo systemctl stop agence-worker agence-web
sudo -u agence bash -c 'gunzip -c /srv/agence-ia/sauvegardes/agence-YYYYMMDD-HHMMSS.sqlite3.gz > /srv/agence-ia/agence.sqlite3'
sudo rm -f /srv/agence-ia/agence.sqlite3-wal /srv/agence-ia/agence.sqlite3-shm
sudo systemctl start agence-worker agence-web
```

## Useful commands on the VPS

```bash
sudo agence disque                       # disk space and alerts
sudo agence sauvegarde                   # immediate database backup
sudo agence notification sujet           # create the secret ntfy topic for push notifications
sudo agence notification test            # send a test notification
sudo agence rush ajouter /tmp/photo.jpg  # the file must be readable by the agence user (e.g. in /tmp)
sudo agence rush lister
sudo agence corbeille lister
sudo agence tache lister
systemctl list-timers agence-purge.timer     # next purge
journalctl -u agence-purge.service -n 50     # latest purges
sudo -u agence nano /srv/agence-ia/app/.env  # edit the configuration
```

To send a photo from your computer: `scp photo.jpg user@your-vps:/tmp/`.
