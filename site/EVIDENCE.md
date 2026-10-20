# Evidências da página · 17/10/2026

Esta é uma fotografia da instalação operada pelo autor. Não é benchmark,
promessa de resultado para outra instalação ou prova de que a distribuição pública está pronta.

## Produção

- Fonte: registro operacional `claude/uso.jsonl`, primeiro registro em 08/10/2026 às 00:46:35 UTC.
  Em 17/10 há mais de sete dias de histórico: a página usa o limite conservador de **pelo menos uma semana**.
- Fonte: cadastro `sites/*.json`, lido por `hub.sites.ativos()` em 17/10/2026: **6 negócios** ativos.
  A autorização de divulgação cobre cinco nomes; a página não identifica o sexto.
- Os links dos projetos demonstram os negócios operados. Não são depoimentos de clientes,
  endossos profissionais nem prova isolada da autoria de cada mudança nesses sites.

## Execuções

Fonte: `claude/uso.jsonl`, consulta em 17/10/2026. Agrupamento pelo dia UTC de `fim`,
ou `inicio` quando não há `fim`. Sucesso: `ok` verdadeiro e `interrompida` falso.

| Dia UTC | Registros | Sucessos sem interrupção |
| --- | ---: | ---: |
| 15/10/2026 | 1.342 | 1.316 |
| 16/10/2026 | 1.043 | 1.038 |

Inclui agentes e rotinas, retomadas e múltiplas execuções da mesma tarefa. Não equivale a
issues Done, entregas finais, pessoas ou negócios. Logs brutos são privados e não foram publicados.
Não publicamos a queda de 72% de custo por tarefa: essa metodologia não foi reproduzida nesta auditoria.

## Funcionalidades conferidas no código do hub

Referência interna de auditoria: snapshot `efab9d4`, conferido em 17/10/2026.
Os caminhos abaixo identificam as fontes internas; não alegam disponibilidade no repo público.

| Funcionalidade | Fonte interna |
| --- | --- |
| Linear, faixas, contas, transbordo por cota e retomada sem IA | `hub/linear.py`, `hub/agenda.py`, `hub/conta.py` |
| Revisão, CI, merge, publicação e portão de Done | `hub/recibo.py`, `hub/portao_done.py`, `hub/esperas.py` |
| Segundo cérebro e mapa | `hub/grafo.py`, `hub/grafo_tela.py`, `hub/cerebro.py`, `hub/wiki_compilador.py` |
| Projetos e BI por projeto | `hub/sites.py`, `hub/bi_painel.py`, `hub/bi_ingest.py` |
| Auditoria em rodízio e correções elegíveis | `hub/seomator.py`, o agendador do deploy |
| Ranking, velocidade de campo e visibilidade em IA | `hub/ranking.py`, `hub/bi_painel.py`, `hub/bi_ingest.py`, `hub/velocidade_campo.py`, `geo_tracker.py`, `geo_new.py` |
| Receitas no despacho | `hub/biblioteca_indice.py`, `hub/biblioteca_uso.py`, `hub/linear.py` |
| Vigilância e debugger | `hub/vigia_deploy.py`, `hub/debugger.py` |

BI depende das integrações habilitadas em cada projeto. A auditoria SEO roda de madrugada em
lotes e alterna seções para cobertura semanal; não é uma varredura integral simultânea de todos os sites.
Correções seguem os limites de autorização, revisão e publicação do projeto.

## Identidade

Ampulheta aprovada em 14/10/2026, copiada do ativo vetorial original o ativo vetorial do projeto.
Ícone: `o ativo vetorial original do ícone`. Fontes Inter e Instrument Serif locais, com licenças em `assets/fonts/`.
Contato: e-mail público do autor do repositório. Razão social e CNPJ: decisão de publicação do autor.

## Instalação

Os cinco comandos da página vêm de `docs/INSTALAR.md` e do `README.md` do repositório e foram conferidos no código: entrypoint `krohnos` (`pyproject.toml`), subcomandos `init` e `demo` (`hub/cli.py`), rota `/saude` (`hub/painel.py`) e o serviço `painel` no `compose.yml`. O teste de instalação cronometrada (`scripts/teste-instalacao.sh`) não foi executado para esta página: é a prova de máquina limpa e continua pendente.

Atualização de identidade em 17/10/2026: preto e verde profundo, conforme decisão do autor. A geometria da ampulheta aprovada foi preservada, com as cores ajustadas à nova paleta.

## Preços e comparação · consulta em 20/10/2026

Todos os valores da seção de comparação e do custo típico foram lidos nas páginas oficiais em 20/10/2026, em dólar
(exceto o servidor, em euro, sem IVA). Mudam sem aviso: a página linka cada fonte.

| Item | Fonte oficial | O que foi lido |
| --- | --- | --- |
| Semrush | semrush.com/prices | SEO US$ 139 (US$ 117,33 no anual), Starter US$ 199 (165,17), Pro+ US$ 299 (248,17), Advanced sob consulta; usuário extra a partir de US$ 45 |
| Ahrefs | ahrefs.com/pricing | Lite US$ 129, Standard US$ 249, Advanced US$ 449, Enterprise US$ 1.499; limites e histórico (6 meses, 2 anos, 5 anos) |
| Similarweb | similarweb.com/corp/pricing | sem preço publicado; pacotes sob contato comercial |
| DataForSEO | dataforseo.com/pricing, /apis/serp-api/google-serp-api, /apis/backlinks-api | sem assinatura, depósito mínimo US$ 50; SERP US$ 0,0006 / 0,0012 / 0,002; Backlinks US$ 0,024 por requisição + US$ 0,000036 por linha; OnPage a partir de US$ 0,00015 por página |
| Claude | claude.com/pricing | Pro US$ 20 (US$ 17 no anual); Max a partir de US$ 100; Claude Code incluído |
| Codex | learn.chatgpt.com/docs/pricing | Codex incluído no ChatGPT; Plus US$ 20; Pro a partir de US$ 100 |
| Linear | linear.app/pricing | Free US$ 0 (250 issues, 2 times); Basic US$ 10 por usuário no anual |
| Servidor | hetzner.com/cloud | a partir de € 5,99 por mês sem IVA; a página não lista a RAM de cada plano |
| Google | support.google.com/webmasters/answer/9128668; marketingplatform.google.com/about/analytics | Search Console e Analytics gratuitos |
| Clarity | clarity.microsoft.com | gratuito, sem limite de tráfego |

Limites desta consulta: a página do PageSpeed Insights consultada não declara preço nem cota, por isso a tabela diz só "chave do Google Cloud".
O cálculo de DataForSEO (400 SERPs × US$ 0,0006 = US$ 0,24) é um exemplo aritmético, não uma medição. O teto de US$ 2 por dia e as
estimativas por resposta de IA (US$ 0,004, 0,04 e 0,18) vêm das constantes `TETO_DIA_USD` (`hub/dataforseo.py`) e `PRECO`
(`hub/mcp_grupos/visibilidade_ia.py`). As 373 regras do SEOmator vêm da descrição do pacote `@seomator/seo-audit` 6.0.0 (licença MIT, conferida no registro npm).

## Etapas do orquestrador

Cada etapa da seção "Como o orquestrador funciona" cita arquivos que existem no repositório público; o teste `test_every_process_links_an_existing_file`
confere um a um. O script de revisão do Codex (`codex-revisar`) ainda não está no repositório e a página diz isso.
Os números da auditoria do portão de Done (873, 9, 7 e 187) estão na docstring de `hub/portao_done.py`.
