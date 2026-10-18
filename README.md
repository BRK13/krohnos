<p align="center"><img src="docs/kronos-loading.gif" width="96" alt="Pereiraos hourglass"></p>

<h1 align="center">Pereiraos</h1>

<p align="center"><b>The harness that turns AI coding agents into a team you can run from your phone.</b></p>

<p align="center"><a href="README.pt-BR.md">Leia em português</a> · <a href="docs/harness.md">The harness</a> · <a href="docs/ARQUITETURA.md">Architecture</a> · <a href="ROADMAP.md">Roadmap</a> · License: AGPL-3.0</p>

---

**One person. Six live products. Four Claude Code accounts and Codex as reviewer.
1,309 pull requests merged in 7 days (Oct 7 to 14, 2026), driven from a phone.**

Pereiraos is the system behind those numbers. It is not a new model or a new agent. It is the *harness* around the
agents: the queue, the rules, the gates and the memory that make many agents produce work you can trust.

> A prompt changes one attempt. A harness changes every attempt.

## What it does

```
You (WhatsApp / panel / Linear)
   │  plain-language request
   ▼
Orchestrator ──► task contract (outcome, scope, what must not change, proof of done)
   │
   ▼
Queue (Linear) ──► 4 agent accounts, one git worktree each (builder / operator lanes)
   │
   ▼
Gates: tests (local CI) ─► second-model review (Codex) ─► merge ─► deploy ─► "is it live?" check
   │
   ▼
Receipt back to you: what changed, the evidence, what is still open
```

- **Task contracts, not prompts.** Every task says what must exist when it is done and how to prove it.
- **"Done" requires evidence.** A merged PR that is live, the file that was asked for, a check that passes. An agent saying "done" is not evidence.
- **Agents never poll.** Waiting for CI or a deploy ends the session; the hub wakes the agent when the status changes.
- **Stuck work raises an alarm.** Every run has a budget (time, attempts, spend) and an escalation rule.
- **Rules that matter become code.** If a mistake happened twice, it becomes a test, a hook or a filter, not one more line in a prompt.
- **Every failure upgrades the harness.** Incidents are logged with the part of the system that changed because of them.
- **Goals.** Give it an objective, a way to measure it and an effort level; agents change one thing at a time, keep what beats the best result by 5%, and chart the progress until the goal is met.
- **One panel.** Business metrics, SEO, agent usage, servers, goals, and what every agent is doing right now.

## Quickstart

You need a Linux machine with Docker 24+ and the Docker Compose v2 plugin. These are the same five steps as
[docs/INSTALAR.md](docs/INSTALAR.md) (that guide is written in Portuguese; the commands are the same).

```bash
# 1. get the code
git clone https://github.com/BRK13/krohnos.git kronos && cd kronos

# 2. build the image (one image serves the 4 services)
docker compose build painel

# 3. configure: `kronos init` creates kronos.toml, the master key, the vault and YOUR owner password
docker compose run --rm --no-deps --entrypoint kronos painel init

# 4. start the 4 services (panel, schedule, webhook, mcp)
docker compose up -d

# 5. check and sign in
curl -fsS http://127.0.0.1:8787/saude      # must answer: ok
```

Then open the panel and sign in with the owner password. Put a TLS proxy in front of it before exposing it to the
internet; do not start the panel on the internet before running `kronos init`.

### Try it with demo data

`kronos demo` fills a fresh install with invented data so you can look around before connecting anything: the two example
projects, 30 days of made-up search and traffic numbers, three goals, two proposals and one delivery receipt. Everything
is marked as demo, every page shows a "demo data" banner while it exists, and nothing runs on its own. The command is:

```bash
docker compose run --rm --no-deps --entrypoint kronos painel demo
```

- The numbers are deterministic: `--semente N` picks the seed (same seed, same numbers). `--dados DIR` picks another data folder.
- Running it twice does not duplicate anything.
- It refuses to run on a data folder that already holds real projects, goals, proposals or metrics, and says why.
- Restart the panel after running it so it reads the new projects: `docker compose restart painel`.
- The demo fills goals, proposals and the project list. The business dashboards (`/bi*`) still only know the original owner's businesses, so they do not show the demo series yet. The demo receipt is just an example file, not a screen.
- To remove only what is marked as demo: `docker compose run --rm --no-deps --entrypoint kronos painel demo --limpar`.

Screenshots of the panel with the demo data (dark and light theme):

![Goals, dark theme](docs/img/painel-metas-escuro.png)
![Goals, light theme](docs/img/painel-metas-claro.png)
![Proposals, dark theme](docs/img/painel-propostas-escuro.png)
![Proposals, light theme](docs/img/painel-propostas-claro.png)

## What is inside

| Part | Where |
|---|---|
| Task contracts, queue, accounts and lanes, receipts, stuck-work alarms, goals | `hub/` (see [docs/ARQUITETURA.md](docs/ARQUITETURA.md)) |
| The panel (stdlib HTTP server, no framework) | `hub/painel.py`, `hub/ui.py` |
| Secrets vault and login | `hub/comum/`, `hub/segredos_fonte.py` |
| Plugins (extra pages, menu items, routines) | `hub/plugins.py` (see [docs/PLUGINS.md](docs/PLUGINS.md)) |
| Docker deploy: one image, four services | `Dockerfile`, `compose.yml`, `deploy/` |
| Example projects | `exemplos/` |

## Documentation

- [docs/harness.md](docs/harness.md): the ideas behind the harness.
- [docs/ARQUITETURA.md](docs/ARQUITETURA.md): each piece of the harness and the files that implement it.
- [docs/INSTALAR.md](docs/INSTALAR.md): full install guide (Portuguese).
- [docs/PLUGINS.md](docs/PLUGINS.md): how to write a `kronos_plugin_*`.
- [ROADMAP.md](ROADMAP.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Status

This repository is the public home of Pereiraos. The code is being extracted from a private production setup and
cleaned of anything specific to its first owner. See [ROADMAP.md](ROADMAP.md) for what ships first:
the standalone **`kronos-harness`** plugin for Claude Code, then the full hub with a 15-minute install.

What does not exist yet, so you do not look for it:

- the standalone `kronos-harness` plugin (the harness pieces are in `hub/`, not yet packaged on their own);
- the local check script `scripts/checar.sh` (frente 4) that maintainers and contributors run before a pull request;
- external pull requests: they are not accepted until the CLA/DCO decision (see [CONTRIBUTING.md](CONTRIBUTING.md)).

⭐ Star or watch the repo to follow the first release.

## License

[AGPL-3.0](LICENSE). Use it, change it, self-host it. If you offer a modified version as a service, share your changes.
