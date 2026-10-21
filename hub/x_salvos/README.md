# Agente de conhecimento do X

Transforma os posts que você salvou (bookmarks) no X em receitas curtas de conhecimento. Uso pessoal: lê a sua própria conta,
pela sua própria sessão, em modo somente leitura.

## Aviso de termos

Esta ferramenta lê só os salvos (bookmarks) da sua própria conta, pela sua sessão, em modo somente leitura; a busca pública fica
fora deste agente. Sem scraping em massa: no máximo 500 itens por coleta e uma coleta por dia (`--forcar` serve para a carga
inicial ou uma correção e também respeita o teto). Você é responsável por cumprir os termos de uso do X. O texto do aviso sai em
`--help` e na primeira rodada.

## Como funciona

```
salvos do X (bookmarks)
   |  coletar: navegador sem tela, cookies da sua sessão, 1 coleta por dia, somente leitura
   v
<saida>/AAAA-MM-DD.jsonl  (+ vistos.json: ids já coletados; ERRO se a sessão cair)
   |  extrair: o post vira DADO dentro de um bloco com delimitador aleatório; `claude -p` só texto: sem ferramentas, sem MCP, sem config de usuário
   v
receita validada  (frontmatter, tamanho, sem URL fora dos links do post, sem comando perigoso)
   |  destino = "biblioteca": <saida>/receitas/<tema>/<id>-<titulo>.md   (status: curta)
   |  destino = "issue":      <saida>/issues/<id>-<titulo>.md
   v
uso: a receita entra na biblioteca de referências, ou vira texto de issue, e é aplicada em trabalho real
```

Cada passo é idempotente: salvo já visto não é coletado de novo e receita já gravada não chama o LLM outra vez.

## Instalar

0. O código é o módulo `hub/x_salvos/` do repositório `example-org/krohnos`: `git clone https://github.com/BRK13/krohnos.git` e rode todos os comandos abaixo na raiz do clone.
1. Python 3.11 ou mais novo e um Chrome ou Chromium instalado. Para `extrair`, o CLI `claude` instalado e autenticado (ele roda
   como `claude -p --tools "" --strict-mcp-config --setting-sources ""`: só texto, sem ferramentas, sem servidores MCP e sem as
   configurações nem hooks de usuário e projeto), ou a sua própria função `executar_llm(prompt)` se você chama o módulo pelo Python.
2. `pip install playwright` (só o pacote; o navegador é o do sistema, apontado por `chrome`).
3. Exporte os cookies (próxima seção) e crie o `x_salvos.toml`.
4. Teste à mão: `python3 -m hub.x_salvos coletar --config x_salvos.toml`, depois `python3 -m hub.x_salvos extrair --config x_salvos.toml`.
5. Para rodar todo dia, agende `python3 -m hub.x_salvos coletar --jitter --config x_salvos.toml` no cron do sistema (`--jitter` espera de
   0 a `jitter_max_s` antes de começar; à mão, rode sem ele).

## Como exportar os cookies

O agente nunca faz login: usa a sessão que você já tem aberta no navegador.

1. Entre no X no navegador, com a sua conta.
2. Com uma extensão que exporta cookies, ou pelo DevTools (Application, Cookies, https://x.com), copie só o valor de
   `auth_token`, `ct0` e `twid`.
3. Grave um JSON com apenas essas três chaves:
   `{"auth_token": "...", "ct0": "...", "twid": "..."}`
4. `chmod 600` no arquivo. Um arquivo mais aberto é corrigido e avisado, e o valor nunca aparece no log.
5. Arquivo com campo de login conhecido (senha, usuário, email, login, código 2FA/OTP, telefone) é recusado; a lista é fixa em `CHAVES_DE_LOGIN`, então mantenha só as três chaves.

Quando a sessão expira, a coleta grava `ERRO` na pasta de saída e para (código 2): exporte cookies novos.

Cookies exportados em outra máquina: aponte `cookies_origem` para o arquivo (ou para a pasta onde ele chega, com o mesmo nome do
arquivo de `cookies` ou, na falta dele, `x-cookies.json`). A cada coleta, se a origem for mais nova que `cookies`, ela é copiada com `chmod 600`, só com os três
cookies, e o log diz apenas que atualizou (provado por `TestCookiesOrigem`).

## Configuração (`x_salvos.toml`)

Precedência: padrões < arquivo < variáveis de ambiente. Chave desconhecida é erro. Todas as chaves (valores de `cookies` e `saida` abaixo são exemplo; os padrões do código são `~/.config/krohnos/x-cookies.json` e `~/krohnos/x-salvos`):

```toml
cookies = "~/.config/x-salvos/cookies.json"   # JSON com auth_token, ct0, twid (chmod 600)
cookies_origem = ""                           # opcional: arquivo ou pasta onde os cookies chegam de outra máquina
saida = "~/x-salvos"                          # jsonl, vistos.json, ERRO e receitas
temas = ["seo", "automacao"]                  # opcional: receita fora dos temas vai marcada, nunca é descartada
destino = "biblioteca"                        # "biblioteca" ou "issue"
max_itens = 150                               # itens por rodada (máximo 500)
max_paginas = 40                              # rolagens por rodada
limite_taxa_s = 1.5                           # intervalo mínimo entre navegações, em segundos
jitter_max_s = 1200                           # com --jitter: espera de 0 a este valor antes de começar
chrome = "/usr/bin/google-chrome"
trava = "~/x-salvos/.trava"                   # arquivo do flock contra duas coletas; padrão <saida>/.trava
```

Variáveis de ambiente: `KROHNOS_X_SALVOS_CONFIG`, `KROHNOS_X_SALVOS_COOKIES`, `KROHNOS_X_SALVOS_COOKIES_ORIGEM`,
`KROHNOS_X_SALVOS_SAIDA`, `KROHNOS_X_SALVOS_TEMAS`, `KROHNOS_X_SALVOS_DESTINO`, `X_MAX_ITENS`, `KROHNOS_X_SALVOS_MAX_PAGINAS`,
`KROHNOS_X_SALVOS_LIMITE_TAXA_S`, `KROHNOS_X_SALVOS_JITTER_MAX_S`, `KROHNOS_X_SALVOS_CHROME`, `KROHNOS_X_SALVOS_TRAVA`.

## Guardrails e o teste que prova cada um

Todos em código (`guardrails.py`), e cada um falha se o código for removido. Testes em `tests/test_x_salvos_guardrails.py`,
`tests/test_x_salvos_extrair.py` e `tests/test_x_salvos_config.py`.

| Guardrail | O que garante | Teste |
|---|---|---|
| Somente leitura | método de escrita e endpoint de escrita (inclusive em GET) são abortados; sem corpo no registro | `TestSomenteLeitura.test_metodos_de_escrita_bloqueados`, `test_endpoints_de_escrita_bloqueados_mesmo_em_get`, `test_instalar_aborta_escrita_e_registra_sem_corpo` |
| Somente leitura | o único clique permitido é expandir respostas; ação (curtir, repostar, seguir) é recusada | `test_clique_de_acao_recusado` |
| Somente leitura | URL ilegível, codificação ou subdomínio não abrem brecha; falha fecha | `test_url_ilegivel_aborta_fail_closed`, `test_brechas_de_codificacao_e_subdominio` |
| Somente leitura | a trava está no contexto do navegador, inclusive em `--refazer-fio`; Service Workers são bloqueados (o `route` não os vê) | `test_abrir_instala_a_trava_no_contexto`, `test_refazer_fio_abre_pelo_abrir` |
| Sessão é sua | só `auth_token`, `ct0`, `twid`; campo de login conhecido é recusado | `TestSessaoDoUsuario.test_cookies_so_os_tres`, `test_rejeita_credencial_de_login`, `TestCookiesOrigem.test_login_ampliado_recusado` |
| Sessão é sua | não existe fluxo de login e a coleta não navega para a tela de login | `test_fonte_sem_fluxo_de_login`, `test_nao_navega_para_login` |
| Sessão é sua | origem de cookies copiada só se mais nova, com 600, sem valor no log, sem credencial de login; origem ilegível vira ERRO com código 2 | `TestCookiesOrigem` |
| Limite de taxa | intervalo mínimo entre navegações | `TestLimiteETeto.test_intervalo_minimo_entre_navegacoes`, `test_ir_respeita_a_taxa` |
| Teto | teto de rolagens e de itens por rodada | `test_teto_de_rolagens`, `test_teto_de_itens` |
| Sessão caída | detecta por URL e para sem insistir; teto de rolagens termina normal | `test_sessao_caiu_por_url`, `TestParada` |
| Segredos | valor mascarado em qualquer log e em ERRO, inclusive de exceção inesperada (código 2) | `TestSegredos.test_mascarar`, `test_log_nunca_mostra_valor`, `test_erro_gravado_vai_mascarado`, `TestMainExcecao` |
| Segredos | cookies gravados com 600 e de forma atômica; permissão aberta é corrigida | `test_gravar_cookies_600_atomico`, `test_permissao_mais_aberta_e_corrigida_sem_valor`, `test_ler_cookies_corrige_permissao` |
| Termos | aviso nos pontos certos, teto absoluto de 500 itens por coleta (também em `--forcar` e `--refazer-fio`), uma coleta por dia, uma coleta por vez (`trava`) | `TestTermos.test_aviso_tem_o_essencial`, `test_ajuda_e_primeira_rodada_imprimem_aviso`, `test_teto_500`, `test_uma_coleta_por_dia`, `TestColetorPuro.test_trava_impede_segunda_coleta`, `test_main_usa_a_trava_da_config`, `TestRefazerFioTeto` |
| Post é dado | texto do post fica dentro de bloco com delimitador aleatório; delimitador forjado é neutralizado | `TestPrompt` em `test_x_salvos_extrair.py` |
| Resposta validada | comando perigoso, URL fora dos links do post, formato ou tamanho inválido: a receita é rejeitada e o resto segue | `TestValidacao`, `TestDestino.test_resposta_perigosa_nao_grava_e_pipeline_segue` |
| LLM sem poder | `claude -p` só texto: sem ferramentas, sem MCP, sem config de usuário/projeto, sem pular permissões | `TestDestino.test_comando_padrao_sem_ferramentas_mcp_nem_configuracao_de_usuario`, `test_padrao_nao_usa_bypass` |
| Configuração | destino e teto inválidos são recusados antes de coletar | `tests/test_x_salvos_config.py` |

Rodar a prova: `python3 -m unittest tests.test_x_salvos_config tests.test_x_salvos_guardrails tests.test_x_salvos_extrair`.
