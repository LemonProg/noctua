# Agent architecture

> Agent and message identifiers are kept in French, exactly as they appear in the code.

## Principle

A **marketing** agency (organic content for Instagram) runs on a small VPS: a team of Claude agents produces, a human approves.

Every exchange is a **typed message** written to a **shared task queue** and addressed to one agent. Agents never call each other directly: they read their tasks from the queue and write their results back to it.

## Flow

```text
planner (Sunday) ──► agent_analyste ──► report ───────────────┐
                 ──► agent_veille ────► market report ────────┤
                                                              ▼
                                               orchestrateur_marketing
                                    ┌──────────────┼──────────────┐
                               brief_visuel    brief_video    brief_texte
                                    ▼              ▼              │
                              agent_visuel    agent_video         │
                                    └──── asset ───┴──────────────┤
                                                                  ▼
                                                          agent_redacteur
                                                                  ▼
                                                  agent_relecteur ──► corrections ──► orchestrator
                                                                  ▼
                                                     human approval queue
                                              approved ▼                ▼ rejected
                                          agent_publication       agent_erreurs ──► lesson (to approve)
                                                  ▼
                                              Instagram ──► statistics ──► agent_analyste
```

## Agents

| ID | Model / tool | Receives | Produces |
|---|---|---|---|
| `orchestrateur_marketing` | Opus 5.5 | weekly trigger, market report, analyst report, reviewer corrections, optional human brief | editorial calendar, `brief_visuel`, `brief_video`, `brief_texte` (each with model and effort) |
| `agent_veille` | Opus 5.5 + web search | weekly trigger | market report: competitors, formats and angles that work, cadence, recommendations, sources |
| `agent_visuel` | Opus 5.5 + headless Chromium | visual brief + footage + brand guide + art direction + style references; style analysis | asset (image or carousel) + the HTML used; proposed art direction |
| `agent_video` | Opus 5.5 + HyperFrames | video brief + footage + brand guide | video asset (MP4) + script |
| `agent_redacteur` | Opus 5.5 | text brief + assets + brand guide | draft post (caption, media, network, schedule) |
| `agent_relecteur` | Sonnet 5.5 (medium effort by default) | draft post + brand guide | "to approve", or corrections |
| `agent_erreurs` | Sonnet 5.5 | rejected content + brand guide + lessons | proposed lesson (approved by the human) |
| `agent_analyste` | Sonnet 5.5 (medium effort by default) | analysis request (Sunday 7 pm) + post statistics (manual, Meta Business Suite import, then API) | weekly report, read by the orchestrator when planning |
| `agent_publication` | no model (official Meta API) | publication request (approved post whose time has come) | post published on Instagram, with date and link |

## Messages

| Message | From → To | Minimum content |
|---|---|---|
| `objectif` | human → orchestrator (optional) | goal, period, networks |
| `demande_veille` | planner → agent_veille | period; model and effort |
| `demande_planning` | planner or human → orchestrator | target week, number of posts; model and effort |
| `rapport_veille` | agent_veille → orchestrator | period, competitors, formats and topics that work, observed cadence, recommendations, sources |
| `brief_visuel` / `brief_video` / `brief_texte` | orchestrator → agent | id, goal, format, network, footage to use, planned date |
| `asset` | visual / video agent → writer | brief id, type, file(s) (one image or carousel slides), prompt or script |
| `analyse_style` | human → agent_visuel | style references to analyse; produces an art direction to approve |
| `post_brouillon` | writer → reviewer | caption, media, network, planned time |
| `corrections` | reviewer → orchestrator | id, list of issues |
| `rapport` | analyst → orchestrator | period, key figures, recommendations, summary, what works, what doesn't |
| `contenu_rejete` | reviewer / human → agent_erreurs | content id, rejection origin (human refusal, "do not use", reviewer corrections), content, brief, comment or corrections |
| `demande_analyse` | planner or human → agent_analyste | target week, force (analyse even without new statistics); model and effort |
| `demande_publication` | worker → agent_publication | id of the approved post (created when its time comes, between 8 am and 10 pm) |
| `lecon` | agent_erreurs → human (approval queue) | content id, identified error, probable cause, rule to apply, agents concerned |

## Task statuses

`à_faire` (to do) → `en_cours` (in progress) → `à_valider` (to approve) → `validé` (approved) or `refusé` (rejected)

Two technical statuses complete the cycle:

- `terminé` (done): intermediate task that needs no human approval (e.g. a brief turned into an asset).
- `échec` (failed): task in error after 3 attempts, with its last error. Visible in the interface; can be retried by hand.

Rules:

- Content rejected by the reviewer goes back to the orchestrator with its corrections before reaching the approval queue.
- Content rejected by the human goes back to the orchestrator with the comment.
- Any rejected content also produces a `contenu_rejete` message for `agent_erreurs`. The proposed lesson goes through the approval queue; only lessons approved by the human enter `lecons.md`.
- Trashed content is deleted only once its analysis is finished. If the analysis is not done after 24 h, the delay is extended as long as disk space allows. If the disk drops below the alert threshold, the interface warns the human, who chooses: delete without analysis or free up space.

## Publication

- Only a post with status `validé` can be published, and only once: publication is recorded in the database right before the irreversible call to Meta. Approving a post authorises its publication at its planned time (editable when approving, like the "made with AI" label).
- Automatic publication is enabled by `PUBLICATION_AUTOMATIQUE`; otherwise, or on failure, the post is downloaded from the calendar and published by hand ("Mark as posted").
- No browser automation: only Meta's official API. Images are exposed while Meta fetches them, at a public URL with a random name, then deleted.

## Models

- Agents call Claude through the human's **Claude subscription**: the `claude -p` command (Claude Code, non-interactive), authenticated with `CLAUDE_CODE_OAUTH_TOKEN`, without tools, with an enforced JSON response. No Anthropic API key.
- `agent_video` is a motion designer: Claude writes the reel in HTML/CSS/GSAP, rendered by HyperFrames (one render at a time). Sound effects: real CC0 recordings from the sound bank (found on Openverse, licence re-checked on Freesound, sorted by ear by the human), with their hit point synced to the picture; as a fallback, at most 2 sounds synthesised with Web Audio. After rendering, loudness is set with a linear gain (-16 LUFS target, -1.5 dBTP peak, configurable), without re-encoding the picture. No voice, no music.
- `agent_visuel` is a graphic designer: Claude writes each visual (image or carousel slides) in HTML/CSS, following the approved art direction and style references; headless Chromium turns it into a PNG (no network access, one render at a time); Claude looks at the render and fixes it before submitting. Photos from the footage library are placed as-is (cropping, simple adjustments), never redrawn. No paid image API.

## Choosing model and effort

Everything runs on the Claude subscription: its quota (5-hour and 7-day windows) is the resource to spare.

- Each message to an agent can carry `modele` (claude-haiku-5-5, claude-sonnet-5-5, claude-opus-5-5) and `effort` (low to max). **The orchestrator always sets them**, choosing the most suitable model and effort for each task.
- **Content generation always uses claude-opus-5-5** (`brief_texte`, `brief_visuel`, `brief_video`, `asset`): any other model is refused when the message is validated.
- Defaults when nothing is set: writer Opus 5.5 medium; designer Opus 5.5 high (review medium); style analysis Opus 5.5 high; rejection analysis Sonnet 5.5 medium.
- Guidelines for the orchestrator:
  - claude-haiku-5-5: simple, short tasks (sorting, classification, extraction, mechanical checks).
  - claude-sonnet-5-5: medium-difficulty internal analysis and writing (rejection analysis, review, statistics summary, routine market watch).
  - claude-opus-5-5: complex or high-stakes tasks (strategy, art direction) and, always, any content generation.
  - Effort: low for mechanical work, medium by default, high for creation and hard reasoning; xhigh and max only when quality truly requires it.
- Above `QUOTA_SEUIL_POURCENT` (90 % by default) on a window, the agency pauses until the reset. At night, the 5-hour window may be used up to 100 % (it ends before the human wakes up); the weekly threshold still applies.

## Night production

Heavy tasks (market watch, planning, visuals, writing, review, style analysis) run at night, when the subscription is not used by the human (`noyau/creneaux.py`).

- Each task has a slot: `jour` (any time), `nuit` (production that can wait for the night), `nuit_suite` (continuation of a night chain: caption, review, correction of a post produced at night).
- **Window rule**: from `NUIT_DEBUT` (midnight) to `ABONNEMENT_LIBRE_MAX` (9 am), a call is started only if the 5-hour window containing it ends before `ABONNEMENT_LIBRE` (8 am) for new production, before 9 am for the continuation of a started chain. During the day, only `jour` tasks run.
- **Decision**: the orchestrator assigns a brief to the night if the post will be ready in the morning and leaves `MARGE_VALIDATION_HEURES` (2 h) for approval before publication; otherwise it starts right away.
- **Re-evaluation** every minute: a night task that could no longer wait for the next night without missing its publication moves to the day.
- Sunday evening's market watch and planning wait for the night; the statistics analysis stays at 7 pm.

## Shared resources

- `charte.md`: brand guide — tone, colours, audience, banned words, examples of good content. Read by every agent.
- `lecons.md`: lessons drawn from rejected content, approved by the human. Read by every agent.
- Footage library: on the server's disk, accessed only through the `stockage` module (ready for S3). Trash for rejected or "do not use" items (deleted within 24 h); published content deleted 7 days after publication.
- Task queue: the single point of exchange.

## Global rules

1. One agent = one task.
2. Every agent reads `charte.md` and `lecons.md` before acting.
3. Nothing is published without human approval.
4. One render at a time on the VPS.
5. All exchanges go through the task queue.
