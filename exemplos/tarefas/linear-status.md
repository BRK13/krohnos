---
quando: 0 17 * * 5
prioridade: 2
modelo: sonnet
timeout: 30m
repos: exemplo-org/exemplo-loja, exemplo-org/exemplo-clinica
externo: false
avisar: false
descricao: Toda sexta, um update de status em cada projeto do Linear (o que mudou, números, o que vem) (exemplo)
---
Você escreve o status semanal de cada projeto no Linear, em português e sem enrolação.

Para cada projeto, junte da última semana:
- as issues feitas, abertas e travadas;
- os commits e PRs mergeados do repositório;
- os números dos relatórios do hub, comparando com a semana anterior.

Escreva o update com estas partes: **Semana**, **Números**, **Feito**, **Travado / risco**, **Próxima semana**.
Projeto sem nada na semana: uma linha "semana parada". Não invente número: sem fonte, não escreva.
