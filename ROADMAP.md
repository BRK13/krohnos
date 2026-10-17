# Roadmap

## 1. `krohnos-harness` (Claude Code plugin, first release)
Small, installs in one command, useful the next day:
- **Done with evidence:** a task is only "done" with a receipt (PR merged and live, requested artifact present, checks green).
- **Wait without polling:** `voltar-quando` (wake me when CI / deploy finishes) instead of sessions that check every few minutes.
- **Stuck-run alarm with budgets:** time, attempts and spend limits; classify the failure before retrying.
- **Change receipt:** what changed, the evidence, the unresolved risk, for the human and the next session.
- **Goals:** objective + how to measure + effort; one change at a time, keep only what beats the best result by 5%, progress chart.

## 2. Krohnos (full system)
- `docker compose up` + `krohnos init`: from an empty server to the panel with one project in 15 minutes.
- Panel screens: **Projects** (create/edit, verify each data source), **Vault** (variables and secrets, encrypted, never shown again), **Connections** (Linear, GitHub, Google Search Console/GA4, Clarity, PageSpeed, Codex), **Agents** (log in Claude accounts, lanes, model per role, spend limits, live activity), **Routines** and **Goals**.
- Real login with roles (owner, operator, read-only).
- Optional plugins: WhatsApp inbox, SEO routines.

## 3. Public proof
- A live page with the system's own numbers (PRs per day, tasks done with evidence, incidents and what each one changed).
