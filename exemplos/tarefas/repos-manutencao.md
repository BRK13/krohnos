---
quando: 0 3 * * 0
prioridade: 3
modelo: sonnet
timeout: 45m
repos: exemplo-org/exemplo-loja, exemplo-org/exemplo-clinica
externo: false
descricao: Todo domingo de madrugada, atualiza as dependências dos repositórios e abre um PR por repositório (exemplo)
---
Para cada repositório da lista:

1. Atualize o clone base com `git fetch` e crie uma branch nova a partir da principal.
2. Atualize as dependências de patch e minor, rode o build e os testes.
3. Se tudo passar, abra um PR com a lista do que mudou. Se algo falhar, não abra PR e descreva o erro.

Nunca faça merge nem `push --force`.
