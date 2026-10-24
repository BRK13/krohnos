# Marca do Krohnos

Uma marca só, a do site krohnos.com: a ampulheta com areia em cima e embaixo (vidro `#123324`, areia `#58B184`) e o nome
"Krohnos" em Inter 550. O SVG não é desenhado aqui: é o arquivo do site (`site/assets/krohnos-mark.svg` e `site/favicon.svg`
do repositório público do Krohnos). Mudou lá, copie para cá byte a byte; `tests/test_marca.py` trava as cores e a geometria.

- `krohnos.svg`: o símbolo em cor, fundo transparente (o mesmo `krohnos-mark.svg` do site). É a fonte do inline de `ui.simbolo()`.
- `krohnos-icone-claro.svg`: o mesmo símbolo, para superfície clara.
- `krohnos-icone-escuro.svg`: o `favicon.svg` do site: placa verde `#123324` com o símbolo claro `#D7EEE2`; lê bem a 16 px na aba clara e na escura. É o `/favicon.svg` do painel.
- `krohnos-loading.svg`: o símbolo parado com três grãos caindo pelo gargalo (SMIL, sem biblioteca). Com `prefers-reduced-motion` só a ampulheta. No painel o loading é inline (`ui.carregando()`), na mesma geometria.

A prancha antiga (carvão, creme e latão) e o traçador dela estão em `arquivo/2026-10-17-marca-prancha-b/`.
