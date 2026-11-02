# CLAUDE.md — Project rules

Instructions for Claude Code when working on this repository.

## Before each session

Read `README.md`, then `docs/architecture.md`.

## Way of working

- **Plan first** for any new piece of work; one step at a time, tested before the next.
- **Git commit** at the end of each working step, with a clear message.
- **When in doubt, ask** rather than guess, especially about social network APIs and costs.

## Technical rules

- The project targets a VPS with **2 cores and 4 GB of RAM**. Always pick the lightest solution. Justify any new dependency.
- **One render (image or video) at a time**, through the task queue.
- Footage and content are stored on the server's disk. Always go through the `stockage` module, never hard-coded paths.
- **Retention**: work files deleted after processing; published content deleted 7 days after publication; rejected or "do not use" footage and content go to the trash and are deleted within 24 h.
- **Model choice**: every task assigned by the orchestrator sets `modele` and `effort`; content generation always uses Opus 5.5.
- **Claude runs on the subscription**, never on an Anthropic API key: do not add `ANTHROPIC_API_KEY` and strip it from the environment of the `claude` command.
- **Secrets only in `.env`**, never in code or git. Keep `.env.example` up to date.
- Agent and message identifiers are those of `docs/architecture.md`. Do not rename them.
- Every agent reads `charte.md` and `lecons.md` before acting.
- Any rejected content is analysed by `agent_erreurs` before deletion. Lessons enter `lecons.md` only after human approval.

## Forbidden

- Publishing through browser automation (Playwright, persistent sessions, stored credentials).
- Publishing or sending anything without human approval.

## Language

The interface, the agents' prompts and the CLI messages are in French; the documentation is in English.
