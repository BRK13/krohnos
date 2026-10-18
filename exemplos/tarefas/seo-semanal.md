---
quando: 0 9 * * 1
prioridade: 3
modelo: sonnet
timeout: 30m
externo: false
descricao: Toda segunda, confere o SEO básico dos sites cadastrados e lista o que piorou (exemplo)
---
Para cada site ativo do cadastro (`python3 -m hub.sites listar`):

1. Rode o verificador do site e compare com o resultado da semana anterior.
2. Anote páginas que saíram do índice, títulos duplicados e erros de rastreamento.
3. Escreva um relatório curto, com um item por problema e o link da página.

Só relate o que o verificador mostrou. Não altere nenhum arquivo dos sites.
