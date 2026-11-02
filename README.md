<div align="center">

<img src="app/statique/icones/icone-192.png" width="96" alt="Noctua logo">

# Noctua

**An autonomous marketing agency made of Claude agents that works at night while you sleep.**<br>
**Your only job: approve or reject.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![Claude subscription](https://img.shields.io/badge/runs%20on-Claude%20Pro%20%2F%20Max-D97757)
![No API key](https://img.shields.io/badge/Anthropic%20API%20key-none-success)
![2 cores · 4 GB](https://img.shields.io/badge/VPS-2%20cores%20%C2%B7%204%20GB-informational)

[Quick start](#quick-start) · [Deploy](#deploy-to-a-vps) · [Features](#features) · [How it works](#how-it-works) · [Configuration](#configuration) · [Architecture](docs/architecture.md)

</div>

---

Noctua runs a brand's Instagram from end to end. It scouts the market, plans the week, designs visuals and reels,
writes captions, reviews everything and publishes at the planned time. **Nothing goes out without your approval.**

It is **token-less**: every agent calls Claude through **Claude Code with your Claude Pro or Max subscription**.
There is no Anthropic API key and no per-token billing. It all fits on a **2-core / 4 GB VPS** with a single SQLite
database.

> [!NOTE]
> The web interface, the agents' prompts and the CLI are in **French**. The agents write in the language of your
> brand guide.

## Quick start

### Requirements

- Python **3.12+**
- Node.js **22+** (reel rendering)
- FFmpeg
- Google Chrome or Chromium (visual rendering)
- [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) and a **Claude Pro or Max** subscription

### Install

```bash
git clone https://github.com/LemonProg/noctua.git
cd noctua

python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
(cd video && npm ci)

cp .env.example .env
cp charte.exemple.md charte.md
```

### Configure

1. **Use local paths** in `.env`:

   ```dotenv
   NOM_AGENCE=My brand
   DOSSIER_CONTENUS=donnees/contenus
   DOSSIER_TEMP=donnees/tmp
   CHEMIN_BASE=donnees/agence.sqlite3
   DOSSIER_SAUVEGARDES=donnees/sauvegardes
   DOSSIER_CONFIG_CLAUDE=donnees/claude-config
   ```

2. **Connect your Claude subscription.** This does not use an API key:

   ```bash
   claude setup-token    # sign in with your Claude account, then paste the token into .env:
                         # CLAUDE_CODE_OAUTH_TOKEN=...
   ```

3. **Fill in `charte.md`**, your brand guide. The agents refuse to start while any `[À REMPLIR]` marker remains.
   - **Automatically:** give Claude Code [this prompt](prompts/brand-guide-from-website.md) with your website's URL.
     It writes the guide (identity, offers, tone, colours, fonts, visual style) and saves images for the art
     direction.
   - **By hand:** replace every marker yourself.

### Run

```bash
agence mot-de-passe      # set the interface password
agence init              # create the database and folders, check the brand guide
agence web               # interface at http://127.0.0.1:8000
python -m noyau.worker   # in a second terminal: the agents at work
```

> [!TIP]
> To open the interface from your phone on the same Wi-Fi, set `HOTE_WEB=0.0.0.0`. The interface installs on your
> phone's home screen like an app.

## Deploy to a VPS

Requirements: Ubuntu 24.04 or Debian 12, SSH access with `sudo`.

```bash
# from your computer, at the project root (with charte.md filled in)
deploy/envoyer.sh user@your-vps agency.example.com
```

Then on the VPS:

```bash
sudo -u agence nano /srv/agence-ia/app/.env       # CLAUDE_CODE_OAUTH_TOKEN, Instagram…
sudo agence mot-de-passe
sudo systemctl enable --now agence-worker agence-web
sudo bash /srv/agence-ia/app/deploy/verifier.sh   # end-to-end check on a throwaway database
```

You can re-run the installer on every update. It sets up:

- Python, Node.js, Chromium, FFmpeg and Claude Code;
- a dedicated system user, hardened systemd services and a firewall;
- swap;
- automatic HTTPS with Caddy;
- an hourly purge and nightly database backups.

The full guide is in [`deploy/README.md`](deploy/README.md).

## Features

- 🔎 **Market watch.** Every week the agency finds your competitors on the web and reports the formats, angles and
  posting cadence that work.
- 🗓️ **Planning.** It writes a weekly editorial calendar (4 posts by default) with a brief for each post. The
  orchestrator picks the model and effort for each task.
- 🎨 **Visuals.** Images and carousels are written in HTML/CSS and rendered to PNG by headless Chromium. Claude then
  looks at the render and fixes it.
- 🎬 **Reels.** Motion design in HTML + GSAP, rendered by HyperFrames, with real **CC0** sound effects timed to the
  picture and normalised loudness.
- ✍️ **Writing and review.** The agency writes captions, hashtags and timing. A reviewer checks the brand guide,
  spelling and facts: the agents invent nothing.
- 📤 **Publishing.** Approved posts go out on schedule through the **official Meta API**. You can also download a
  post and publish it by hand.
- 📈 **Analytics.** Instagram statistics are collected and summarised in a weekly report that feeds the next plan.
- 🧠 **Learning from rejections.** Each rejection is analysed and becomes a proposed lesson. A lesson applies only
  **after you approve it**.
- 🌙 **Night production.** Heavy work runs after midnight, so your daytime quota stays free for you.
- 📱 **Mobile-first interface.** It installs on your phone and sends push notifications through
  [ntfy](https://ntfy.sh), with quiet hours.

## How it works

### Token-less by design

Each agent runs `claude -p` (Claude Code in non-interactive mode), authenticated with `CLAUDE_CODE_OAUTH_TOKEN`
from `claude setup-token`. Every call is isolated:

- it uses a Claude config folder reserved for the agency and an empty work folder;
- nothing persists between calls: no session, no MCP servers, no slash commands;
- the agents have no tools, except web search for the market watch and image reading for self-review;
- the answer must match a JSON schema and is validated again with Pydantic.

Noctua **removes `ANTHROPIC_API_KEY`** and the related variables from the environment before every call. It can
never switch to pay-per-use billing by accident.

### It respects your subscription

- Noctua reads the subscription quota (5-hour and 7-day windows) from each response and shows it in the interface.
- Above `QUOTA_SEUIL_POURCENT` (90 % by default), the agency **pauses** until the reset.
- Heavy tasks wait for the night. A night call starts only if its 5-hour window **ends before 8 am**, so the agency
  never eats into your morning.
- Models are matched to the task:
  - Haiku for mechanical work;
  - Sonnet for analysis and review;
  - **always Opus for content creation**.

### The weekly cycle

| When | What happens |
|---|---|
| Sunday 7 pm | The analyst collects statistics and writes its report |
| Sunday night | Market watch, then planning, then visuals, reels, captions and review |
| Monday morning | A push notification arrives and you approve or reject from your phone |
| During the week | Each approved post publishes itself at its planned time (8 am – 10 pm) |

### The agents

| Agent | Default model | Role |
|---|---|---|
| `orchestrateur_marketing` | Opus 5.5 | Weekly calendar and briefs |
| `agent_veille` | Opus 5.5 + web search | Market and competitor watch |
| `agent_visuel` | Opus 5.5 + Chromium | Images and carousels |
| `agent_video` | Opus 5.5 + HyperFrames | Reels |
| `agent_redacteur` | Opus 5.5 | Captions and the final post |
| `agent_relecteur` | Sonnet 5.5 | Review against the brand guide and the facts |
| `agent_erreurs` | Sonnet 5.5 | Turns rejections into proposed lessons |
| `agent_analyste` | Sonnet 5.5 | Weekly performance report |
| `agent_publication` | none (Meta API) | Publishes approved posts on schedule |

Agents never call each other directly. They exchange **typed messages** through a SQLite task queue, and a single
worker processes them **one at a time**. Details are in [`docs/architecture.md`](docs/architecture.md).

## Configuration

Copy [`.env.example`](.env.example) to `.env`. When something is wrong, the agency reports every configuration
error at once.

| Variable | Purpose |
|---|---|
| `NOM_AGENCE` | Name shown in the interface, the installed app and notifications |
| `DOSSIER_CONTENUS`, `DOSSIER_TEMP`, `CHEMIN_BASE`, `DOSSIER_SAUVEGARDES` | Where files, the database and backups live |
| `DISQUE_SEUIL_ALERTE_GO`, `DISQUE_SEUIL_REFUS_GO` | Free-space thresholds: first an alert, then new footage is refused |
| `CORBEILLE_DELAI_HEURES`, `PUBLICATION_DELAI_JOURS` | How long trashed and published files are kept |
| `PLAFOND_COUTS_MENSUEL_USD` | Monthly cap for paid APIs (`0` = none allowed) |
| `QUOTA_SEUIL_POURCENT` | Subscription usage level at which the agency pauses |
| `CLAUDE_CODE_OAUTH_TOKEN` | 🔐 Subscription token from `claude setup-token` |
| `DOSSIER_CONFIG_CLAUDE` | Claude Code config folder reserved for the agency |
| `HOTE_WEB`, `PORT_WEB` | Interface listen address |
| `MOT_DE_PASSE_INTERFACE`, `SECRET_SESSION` | 🔐 Written by `agence mot-de-passe` |
| `NTFY_SUJET` | 🔐 Secret ntfy topic for push notifications |
| `PUBLICATION_AUTOMATIQUE`, `INSTAGRAM_API_URL`, `INSTAGRAM_COMPTE_ID`, `INSTAGRAM_JETON` | Publishing through the Meta API |

<details>
<summary><b>Optional settings</b></summary>

| Variable | Default | Purpose |
|---|---|---|
| `URL_PUBLIQUE` | — | Public HTTPS URL. Required for automatic publishing, because Meta fetches the media from it |
| `NUIT_DEBUT` | `00:00` | Start of night production |
| `ABONNEMENT_LIBRE` | `08:00` | Time by which the subscription must be free for you |
| `ABONNEMENT_LIBRE_MAX` | `09:00` | Latest time to finish a chain of tasks that has already started |
| `MARGE_VALIDATION_HEURES` | `2` | Time left for you to approve a post before it publishes |
| `NTFY_SERVEUR` | `https://ntfy.sh` | ntfy server, if you host your own |
| `NOTIFICATIONS_SILENCE` | `22:00-08:00` | Quiet hours |
| `SAUVEGARDES_CONSERVEES` | `7` | Number of backups kept |
| `SON_CIBLE_LUFS`, `SON_CRETE_DBTP` | `-16`, `-1.5` | Reel loudness |

</details>

> [!WARNING]
> **Never set `ANTHROPIC_API_KEY`.** Claude Code would use it instead of your subscription, and you would be billed
> per use.

### Connecting Instagram (optional)

You need:

1. a **professional Instagram account** and a **Meta app** (developers.facebook.com);
2. `INSTAGRAM_COMPTE_ID` and a long-lived `INSTAGRAM_JETON` in `.env`;
3. a **public HTTPS domain** in `URL_PUBLIQUE`. Meta fetches the media from it; each file sits under a random name
   for the time of the publication and is then deleted;
4. `PUBLICATION_AUTOMATIQUE=oui` once everything is ready.

Without Instagram, the agency still works: you download each approved post from the calendar and publish it by hand.

## Make it your brand

- **`charte.md`**: identity, offers, tone, colours, audience, banned words and example posts. The agents use nothing
  that is not written here or in a brief. You can
  [generate it from your website](prompts/brand-guide-from-website.md) in a few minutes.
- **Style references** (Réglages > Références): upload visuals you like. The visual agent turns them into an
  **art direction** that you approve.
- **Fonts**: `.woff2` files in `app/polices-marque/`. This folder is not versioned, because many font licences
  forbid redistribution.
- **Logo**: in [`app/marque/`](app/marque/).
- **Footage**: your real photos and videos, uploaded in the interface or with `agence rush ajouter`. They are used
  **as-is**: never redrawn and never "enhanced" by AI.

## CLI

```text
agence init | web | mot-de-passe | disque | purge | sauvegarde
agence rush ajouter FILES… | lister | ecarter ID
agence corbeille lister | restaurer ID
agence tache creer … | lister
agence statistiques releve
agence notification sujet | test
agence sons importer | lister
agence video banc --durees 15 30 60
python -m noyau.worker
```

Run `agence --help` to see every command.

## Project structure

```text
agents/     one module per agent
noyau/      core: Claude calls, task queue, worker, scheduler, night slots, quota,
            storage, rendering, Instagram, publishing, retention
app/        web interface (FastAPI + Jinja + HTMX, no JS build)
video/      HyperFrames + GSAP (pinned versions)
deploy/     VPS installer, systemd units, Caddy, checks, backups
prompts/    ready-to-use prompts (brand guide from your website)
docs/       architecture of agents and messages
```

These choices keep the agency light enough for a 2-core / 4 GB VPS:

- **7 Python dependencies**;
- **one SQLite database** with versioned migrations;
- **one render at a time**;
- all file access goes through a single `stockage` module, so moving to S3 later won't touch the agents.

## Security and safeguards

- **Nothing goes out without you.** Only an approved post can be published, and only once.
- **No browser automation.** Noctua publishes only through Meta's official API: no Playwright, no stored sessions,
  no stored passwords.
- **No API key.** Anthropic keys are stripped from the environment, and the cost cap is 0 by default.
- **Nothing invented.** Agents use only the brand guide and briefs, and a reviewer checks the facts.
- **CC0 sounds only.** Noctua re-checks each sound's licence on its Freesound page.
- **Protected interface.**
  - scrypt-hashed password and signed session cookie;
  - login rate limiting and security headers;
  - secrets only in `.env`.

## FAQ

<details>
<summary><b>How much does it cost?</b></summary>

Your Claude subscription and a small VPS. Noctua uses no paid API by default. A Max plan leaves more headroom, and
the agency pauses on its own before it uses up your quota.
</details>

<details>
<summary><b>Can it publish without me?</b></summary>

No. Approving a post authorises its publication at its planned time, and nothing else.
</details>

<details>
<summary><b>What if my computer is off at night?</b></summary>

When you run it locally, night production only runs if the machine is on. Otherwise it resumes at the next start.
On a VPS it runs all the time.
</details>

<details>
<summary><b>Is this allowed with a Claude subscription?</b></summary>

Noctua uses Claude Code, Anthropic's official tool, with a token from `claude setup-token`, which is meant for
non-interactive use. Keep to personal use of your subscription, and follow Anthropic's and Meta's terms of use.
</details>

## Contributing

Issues and pull requests are welcome. Please keep to the rules in [`CLAUDE.md`](CLAUDE.md):

- the lightest solution;
- no new dependency without a reason;
- secrets only in `.env`;
- nothing published without human approval.

## License

[MIT](LICENSE).

Bundled third-party assets:

- [Phosphor](https://phosphoricons.com) icons (MIT);
- [Geist](app/statique/polices/LICENCE.txt) font (SIL OFL);
- [htmx](https://htmx.org) (0BSD).

Sound effects come from [Openverse](https://openverse.org) (CC0).
