# Validação · 17/10/2026

- 17 testes focados do gerador, incluindo comandos iguais aos de docs/INSTALAR.md, CNPJ, copy institucional e determinismo.
- Navegador real em inglês e português, 320/390/768/1440 px: sem transbordamento, menu sem
  sobreposição, navegação, tema persistido e nenhuma exceção JavaScript.
- Leitura sem JavaScript e preferência por movimento reduzido verificadas.
- `inspect.mjs` (18/10/2026, rodado por `tests/browser_test.py` com `--allow few-pictures,no-footage`): 0 FAIL nas duas línguas em 1440/768/390 px. Duas regras foram dispensadas por decisão de marca e política, não por medição: `few-pictures` (identidade de ferramenta dev, sem fotografias na tela de entrada) e `no-footage` (sem vídeo gerado automático, decisão do projeto). Três WARN restantes, em EN e PT: `mixed-font-headline` e `font-count` (o título do hero tem `Local control.` em Instrument Serif itálico, a terceira família; não foi pedido na identidade monoespaçada e terminal, é escolha de design a confirmar com o Bruno) e `still-page` (movimento discreto; a fita de luz, `hero-gl`, é o único movimento contínuo).
- Capturas desktop, celular e menu aberto conferidas visualmente. Capturas dos projetos vieram
  das páginas públicas em 17/10/2026, com consentimento opcional de medição recusado quando disponível.
- Não executado build de produção ou suíte completa localmente. Geração temporária somente nos testes focados.
- Publicação e autodeploy ainda exigem revisão do PR, operação administrativa e prova de um merge no ar.
  O HTML final contém `meta[name=krohnos-release]` com `2026-10-17` para a conferência após o deploy.

## Comparação com a SERP real

Consulta via `get_serp_results` do hub em 17/10/2026: `self hosted multi agent orchestrator`,
Google Estados Unidos, inglês, três resultados orgânicos. Os links são omitidos aqui: dois eram artigos ou repositórios técnicos, lidos com HTTP 200, e um era uma discussão cuja leitura direta retornou 403.

Para o leitor que avalia uma operação empresarial concreta, a página oferece evidência local datada,
critérios de conclusão, além da descrição técnica. É essa a vantagem
editorial pretendida; não afirma superioridade técnica sobre concorrentes, cuja documentação pode ser
mais apropriada para implementação. Não foi possível avaliar integralmente a discussão bloqueada.
A busca equivalente em português trouxe uma discussão sobre Claude Code e um curso de OpenClaw.

## Atualização de 20/10/2026 · conteúdo explicativo

- Seções novas em português e inglês: como o orquestrador funciona (13 etapas, cada uma com diagrama e link para o arquivo), SEO com origem e custo de cada dado,
  comparação com Semrush, Ahrefs e Similarweb com preço oficial, link e data, e o que é preciso ter. Fontes em `EVIDENCE.md`.
- O Markdown copiável, `install.md`, `install.pt.md` e `llms.txt` incluem as mesmas seções (testes novos em `test_build.py`).
- `inspect.mjs` (21/10/2026, `--allow no-footage`, medido em EN e PT): as seções têm 5 fotografias reais de domínio público ou CC BY-SA, com crédito por foto (`assets/fotos/credits.json`), e `few-pictures` passa em 768 e 390 px. Em 1440 px o desktop abre só o hero, que não tem foto, e a regra dá FAIL (`Only 0 picture(s)`). Decisão do Bruno (20/10/2026): hero sem foto no desktop, como na decisão de 17/10; `few-pictures` em 1440 px fica como exceção de marca, liberada em `tests/browser_test.py` (pôr 4 imagens no hero quebraria a tela única). WARN restantes: `mixed-font-headline`, `font-count`, `still-page` e `size-count` a 768 px.
- As tabelas viram listas empilhadas no celular, sem rolagem lateral.
- `tests/neutro_termos.txt` ganhou `allow:` para os endereços oficiais de preço citados (o termo proibido aparece só no caminho do link).
