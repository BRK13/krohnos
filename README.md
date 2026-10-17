<p align="center"><img src="docs/kronos-loading.gif" width="96" alt="Kronos Hub hourglass"></p>

<h1 align="center">Kronos Hub</h1>

<p align="center"><b>The harness that turns AI coding agents into a team you can run from your phone.</b></p>

<p align="center"><a href="#português">Leia em português</a> · <a href="docs/harness.md">The harness</a> · <a href="ROADMAP.md">Roadmap</a> · License: AGPL-3.0</p>

---

**One person. Six live products. Four Claude Code accounts and Codex as reviewer.
1,313 pull requests merged in 7 days (Oct 7 to 14, 2026), driven from a phone.**

Kronos Hub is the system behind those numbers. It is not a new model or a new agent. It is the *harness* around the
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

## Status

This repository is the public home of Kronos Hub. The code is being extracted from a private production setup and
cleaned of anything specific to its first owner. See [ROADMAP.md](ROADMAP.md) for what ships first:
the standalone **`kronos-harness`** plugin for Claude Code, then the full hub with a 15-minute install.

⭐ Star or watch the repo to follow the first release.

## License

[AGPL-3.0](LICENSE). Use it, change it, self-host it. If you offer a modified version as a service, share your changes.

---

## Português

**Uma pessoa. Seis produtos no ar. Quatro contas do Claude Code e o Codex revisando.
1.313 pull requests mergeados em 7 dias (7 a 14/10/2026), comandados pelo celular.**

O Kronos Hub é o sistema por trás desses números. Não é um modelo nem um agente novo: é o *harness* em volta dos
agentes. A fila, as regras, os portões e a memória que fazem vários agentes entregarem trabalho confiável.

- **Contrato da tarefa, não prompt:** cada tarefa diz o que tem que existir no fim e como provar.
- **"Pronto" exige evidência:** PR no ar, o arquivo pedido, a checagem passando.
- **Agente não fica consultando:** esperar CI ou deploy encerra a sessão; o hub acorda quando o status muda.
- **Tarefa parada vira alarme**, com teto de tempo, tentativas e gasto.
- **Regra que importa vira código**, não mais uma linha no prompt.
- **Toda falha melhora o harness.**
- **Metas:** objetivo + como medir + esforço, uma mudança por vez, gráfico até bater.
- **Um painel só:** números dos negócios, SEO, uso dos agentes, servidores, metas e o que cada agente está fazendo.

O código está sendo extraído de uma operação privada em produção. Primeiro sai o plugin **`kronos-harness`** para o
Claude Code; depois o hub completo com instalação em 15 minutos. Licença AGPL-3.0.
