# Evidências da página · 17/10/2026

Esta é uma fotografia da instalação operada pela Krohn Capital. Não é benchmark,
promessa de resultado para outra instalação ou prova de que a distribuição pública está pronta.

## Produção e projetos

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
| Auditoria em rodízio e correções elegíveis | `hub/seomator.py`, `deploy/superorganic.cron` |
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

## Capturas dos projetos

Capturas das páginas iniciais públicas em 17/10/2026, viewport 1000 × 650, JPEG local. As imagens não representam a interface do Krohnos; cada uma está ligada ao respectivo projeto.
