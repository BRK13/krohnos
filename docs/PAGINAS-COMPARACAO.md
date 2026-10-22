# Páginas de comparação e citação por IA

Protocolo para escrever, em escala e com portão, as páginas que respondem o que o público busca e pergunta à IA:
"A ou B", "A funciona?", "alternativas a A", sintoma para serviço, pergunta para resposta e comparação com concorrente nomeado.
O guia completo, em português e inglês, está em <https://krohnos.com/pt/guias/paginas-de-comparacao-citacao-ia/>.

O comando é um módulo só, biblioteca padrão do Python (`hub/paginas_comparacao.py`, testes em `tests/test_paginas_comparacao.py`):

```bash
# 1. termos: exportação do Search Console (query, clicks, impressions, position, page) -> lista priorizada
python3 -m hub.paginas_comparacao termos --gsc consultas.csv \
  --concorrentes "Concorrente A,Concorrente B" --lugares "Bairro Alfa,Bairro Beta" --atendemos "Bairro Alfa" \
  --paginas paginas.txt --saida termos.json

# 2. rascunhos: um esqueleto por termo com ação "criar"; nenhum fato é escrito por você
python3 -m hub.paginas_comparacao rascunhos termos.json --pasta rascunhos/ --marca "Marca Exemplo"

# 3. portão: código de saída 1 em qualquer erro; rode antes do pull request
python3 -m hub.paginas_comparacao portao rascunhos/*.md
```

- `--paginas` é um arquivo com uma página por linha, `url<TAB>título`. Busca já coberta por uma página vira "reforçar", não "criar".
- Lugar fora de `--atendemos` vira "descartar": não se cria página de cidade ou bairro sem atendimento real.
- Busca com nome de concorrente vai para os moldes `nominal` e `alternativas`. Esses nascem fora do ar até uma pessoa revisar.
- O portão confere: campos obrigatórios, marcador não preenchido, fato sem fonte, promessa de resultado, superlativo ou 1º lugar
  da própria marca, lugar sem atendimento, linguagem que denigre, "não tem" sobre concorrente, falta de "onde o concorrente ganha",
  regras do conselho profissional (`conselho: CFP|COFFITO|OAB`), data da checagem e páginas irmãs parecidas demais (Jaccard).
- `--idioma en` troca as mensagens; as regras valem nos dois idiomas.

Referências brasileiras para ler antes de publicar comparações (não é aconselhamento jurídico): Código do CONAR, art. 32;
Código de Defesa do Consumidor, arts. 36 e 37; Lei 9.279/1996, art. 195, incisos I e II.
