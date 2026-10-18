---
quando: */15 * * * *
prioridade: 2
modelo: sonnet
timeout: 25m
externo: false
conta: codex
descricao: Revisa e mescla PR autorizado, do mais antigo, com CI e recibo de deploy
---
Faixa codex-revisor-merge. Execute diretamente pelo Codex, sem API e sem subagentes.
Use `python3 -m hub.revisor_merge` em /opt/nimbus/hub para contar In Review e listar issues com label merge,
ordenadas por criação. Guarde o número antes. Pegue até 3 PRs abertos das issues mais antigas elegíveis.

Antes de selecionar PR, confirme que ele implementa a entrega da própria issue (título, corpo e branch).
Menção cruzada em teste, dependência ou incidente não transfere autorização; não devolva a issue por defeito
em PR alheio. PR já MERGED/CLOSED não é candidato a novo merge. Leia o comentário mais recente de reconciliação:
se já há aceite e deploy comprovados da entrega inteira, confira essas provas e reconcilie para Done com recibo;
se há aceite pendente, janela ou gatilho de espera, preserve a espera, sem reenfileirar nem repetir comentários.
Ausência de PR aberto, por si só, não autoriza fechar ou devolver a issue.

Para cada issue, releia labels e estado no Linear imediatamente antes do merge. Só label `merge` autoriza.
Leia AGENTS.md e CLAUDE.md do repo; use worktree própria codex/revisor-<id>. Nunca edite o clone base.
Confira diff inteiro, comentários e revisão do Claude. Faça revisão Codex do código, procurando defeitos sérios.
Exija CI verde no HEAD atual; pendente fica para próxima rodada, sem esperar. Sem CI, rode os testes locais
exigidos pelo repo e guarde a saída. Confira conflito com a base atual; se o repo exige build do combinado,
exija prova do local-ci para esse combinado (nunca rode build ou scripts/ci-local.sh manualmente).
Conflito, revisão com achado sério, falha ou prova insuficiente: comente exatamente o que falta na issue e
devolva para correção; não faça merge.
`hub.linear.reabrir` só constrói a tarefa: não move o estado nem enfileira. Para devolver de fato:
```python
from hub import agenda, linear
t = linear.reabrir(ident, motivo)
issue = linear.gql("query($i: String!) { issue(id: $i) { id team { states { nodes { id name } } } } }", i=ident)["issue"]
estado = next(s["id"] for s in issue["team"]["states"]["nodes"] if s["name"] == "In Progress")
linear.mover(issue["id"], estado, motivo)
agenda.enfileirar(t, "codex-revisor-merge: correção necessária", substituir=True)
```
Use motivo concreto com URL e resultado do CI/conflito/revisão. Não repita comentário idêntico existente.

Tudo verde, revisão sem achado sério e sem conflito: gh pr merge com --match-head-commit <sha verificado>.
Confira deploy uma vez via hub.recibo.no_ar(repo, merge_sha). Se pendente, use hub.linear voltar-quando
<issue> --pr <url> --no-ar; não feche a issue ainda. Só após confirmação do deploy, comente recibo contendo
PR, SHA, revisão, testes/CI e prova do deploy e mova a issue para Done usando hub.linear.mover.
Registre o merge confirmado em /var/lib/nimbus/reports/claude/codex-merges.jsonl (JSON por linha):
{"em":"<ISO UTC>","issue":"KRN-...","pr":"<URL>","sha":"<merge SHA>","deploy":"<prova>"}.
Não registre tentativa ou merge sem deploy confirmado. O painel deduplica por PR.
Conte In Review depois. Registre antes × depois e links nos resultados desta execução. Não mande avisos
individuais ao Fulano: a reúne um aviso com os resultados.
