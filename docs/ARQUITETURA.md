# Architecture of the harness

**Resumo (pt-BR):** o harness é uma esteira de oito peças: contrato da tarefa, fila (`agenda`), contas e faixas
(`conta`), executor (Claude ou Codex), CI, revisão, merge, deploy e recibo. Cada peça tem um módulo em `hub/` e uma
regra que vira código (alarme, teto, prova). Abaixo, um parágrafo por peça e os arquivos de cada uma. O que ainda não
está no repositório público vem marcado `(frente 4)` (script de checagem) ou `(not in the public snapshot yet)`.

```
contract ──► queue ──► accounts & lanes ──► executor ──► CI ──► review ──► merge ──► deploy ──► receipt
 tarefas     agenda      conta, roteamento    Claude /    checar.sh  Codex /   automerge  vigia_deploy  recibo
                                              Codex       local-ci   Claude
                  ▲                                │          │
                  └──── parada, esperas ◄──────────┴──────────┘   (alarm, budget, wake-up on status)
```

Two loops close the circle. Waiting is done outside the session (`esperas`), and a failure becomes an incident that
changes the harness (`incidentes`). Both are described below.

## 1. Contract (issue or task)

Work enters as an issue in the tracker (Linear) or as a scheduled task file (`tarefas/<name>.md`, a small
frontmatter plus the prompt). Either way it carries the outcome, the scope, what must not change and the proof of
done. The prompt every agent receives is the source of its behaviour.

Files: `hub/tarefas.py` (task file contract), `hub/linear.py` (the tracker bridge and the `COMO` prompt),
`hub/propostas.py` (what an agent may do alone versus what needs a go-ahead).

## 2. Queue (`agenda`)

One queue for all agents. A tick every 5 minutes reads the task files, enqueues what is due, and starts at most one
item per free account, by priority and arrival. Two accounts never take the same item or enter the same repository.

Files: `hub/agenda.py`, `deploy/agenda.py` (the command the scheduler calls: `tick`, `rodar`, `enfileirar`, `fila`).

## 3. Accounts and lanes (`conta`)

Each agent account has its own login, its own lock (one job at a time), its own pause when the usage limit is reached,
and a usage log. A lane says what kind of work the account takes: `construtor` (builder: requests from the tracker),
`codex` (requests labelled for the Codex executor) and `operador` (operator: scheduled routine tasks). The lane of each
account is set in `kronos.toml` (`[agentes.faixas]`); an account without a lane takes whatever comes. Routing picks the
model by kind of work.

Files: `hub/conta.py`, `hub/roteamento.py`, `hub/claude.py` (internal calls), `hub/tokens.py` (usage by issue and model).

## 4. Executor (Claude or Codex)

The executor runs in a git worktree of its own, from the contract. Builders write the change; a second model reviews
it. Whoever wrote a change does not review it: `hub/revisar_codex.py` is Claude reviewing pull requests from `codex/`
branches, and the reviewer on the Codex side is `deploy/orquestrador/bin/codex-revisar` (not in the public snapshot yet).

Files: `hub/linear.py` (starts the session), `hub/claude.py`, `hub/revisar_codex.py`.

## 5. CI

Tests run locally, not in a hosted CI. The contributor and the maintainers run `scripts/checar.sh` (frente 4) and the
`/local-ci` status for the pull request. A red base branch is an incident, because every queued pull request would fail
for the same reason.

Files: `scripts/checar.sh` (frente 4), `hub/main_vermelho.py` (red-main detector).

## 6. Review

A second model reviews the final commit of every pull request and comments with findings. The review queue and the
confirmed merges are kept so nothing is reviewed twice or merged unseen.

Files: `hub/revisor_merge.py`, `hub/revisar_codex.py` (Claude reviewing `codex/` branches),
`deploy/orquestrador/bin/codex-revisar` (Codex reviewing the others) (not in the public snapshot yet).

## 7. Merge

A mechanical merge, without a model in the middle: the issue is in review, the pull request is open, CI is green (or the
repository has none) and the review of the tip commit has no serious finding. Once per tip commit.

Files: `hub/automerge.py`.

## 8. Deploy and receipt

After the merge the hub watches the deploy: a stuck or failed deploy is cancelled or redone once, and does not hold the
queue. Then comes the receipt. An issue only stays Done with a receipt: every attached pull request is merged and live,
what the contract asked for exists, a data job shows proof that it ran. Without a receipt the issue goes back to the queue
with the reason.

Files: `hub/vigia_deploy.py`, `hub/recibo.py`, `hub/portao.py` (proof gate for data jobs).

## The two loops

- **Waiting and alarms.** An agent never polls CI or a deploy: it records a wait and ends the session; the hub wakes it
  when the status changes (`hub/esperas.py`). Stuck work has a time, attempt and spend budget and an escalation rule
  (`hub/parada.py`).
- **Every failure upgrades the harness.** Incidents record the class of failure and the part of the harness that changed
  (`hub/incidentes.py`). Goals (`hub/metas.py`) keep an objective, how to measure it and an effort level.

## Extending it

Extra pages, menu items and scheduled routines are plugins (`hub/plugins.py`); see [PLUGINS.md](PLUGINS.md). The panel
is `hub/painel.py` and `hub/ui.py`. The ideas behind all of this are in [harness.md](harness.md).
