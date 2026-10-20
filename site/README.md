# Krohnos · site institucional

Página estática bilíngue, em inglês e português, sobre o orquestrador multiagente self-hosted.
O site apresenta a operação atual; a distribuição pública está em acesso antecipado.
Não publica comandos de instalação sem teste em máquina limpa.

- `content.json`: fonte única do conteúdo nas duas línguas.
- `build.py`: gerador determinístico em Python stdlib, utilizado no CI e no Dockerfile.
- `EVIDENCE.md`: fontes, data, limites e metodologia das métricas e dos preços publicados.
- Blocos de conteúdo: `p`, `ul`, `code`, `install`, `proc` (etapa com diagrama e arquivos do repositório) e `table`.
- `assets/`: fontes locais Inter e Instrument Serif, e ampulheta aprovada.
- `assets/hero-gl.js`: ativo legado preservado, sem carregamento na página.
- `Dockerfile` e `nginx.conf`: geração e publicação pelo painel de hospedagem, com CSP restrita e sem CDN.

## Validação focada

```bash
python3 -m unittest discover -s site/tests -p test_build.py -v
SHOTS_DIR=/caminho/para/prints python3 site/tests/browser_test.py
```

Os testes geram o site em uma pasta temporária. O teste de navegador verifica as duas línguas
em 320, 390, 768 e 1440 px, menu, navegação, ausência de sobreposição/transbordamento,
marca, tema, funcionamento sem JavaScript e links de acesso antecipado.
O ambiente precisa de Playwright e Chromium.

Para executar também a inspeção visual, defina `INSPECT_SCRIPT` com o caminho para `inspect.mjs`.
Nenhuma regra é dispensada. As capturas são gravadas em `SHOTS_DIR` e devem ser conferidas visualmente.
Build de produção e suíte completa pertencem ao runner e ao deploy.

## Publicação

O deploy da landing é configurado no painel de hospedagem: contexto `site`, Dockerfile `Dockerfile`, porta 80.
O identificador técnico `krohnos-release` (`2026-10-17`) nas duas línguas permite conferir qual versão está no ar.
Valide o próximo merge na main conferindo o deploy correspondente e o HTML servido.

## Decisões visuais

Preto e verde profundo conforme atualização do autor em 17/10/2026.
Ampulheta original, com a mesma geometria e cores adaptadas à paleta.
Cabeçalho no fluxo do documento e menu expansível que desloca o conteúdo.
Sem fita de luz; apenas entrada breve de elementos, desativada por `prefers-reduced-motion`.
