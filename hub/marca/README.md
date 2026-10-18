# Marca do Pereiraos

Escolha do Fulano em 14/10: conceito b, a ampulheta em que a areia sobe. A referência é
`B-refinada-escolhida.png`, da entrega em `/home/orch/pc/dados/kronos-entrega-2026-10-14/`.

- `kronos.svg`: o símbolo em cor (carvão `#171717`, areia latão `#B69A64`), com fundo transparente.
- `kronos-icone-claro.svg` e `kronos-icone-escuro.svg`: ícones monocromáticos (carvão sobre marfim `#F2EDE3` e o inverso). Servem de favicon e ficam legíveis em 16 px.
- `kronos-loading.svg`: o loading aprovado pelo Fulano: a ampulheta parada e os grãos subindo, em SMIL e sem bibliotecas. Mantenha a proporção 432:514. Quem pede menos movimento (`prefers-reduced-motion`) vê só a ampulheta.

`kronos.svg` e os dois ícones são o traçado do próprio PNG (potrace, uma camada por cor): refaça com `python3 -I hub/marca/vetorizar.py <B-refinada-escolhida.png> hub/marca`. O loading é o SVG aprovado da entrega, só com a regra de menos movimento. O wordmark KRONOS não está aqui porque a fonte do PNG não é livre. A aplicação no painel (paleta, fonte e `docs/kronos/MARCA.md`) é da.
