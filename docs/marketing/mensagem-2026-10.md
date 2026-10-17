# Base de mensagem do Kronos Hub (outubro de 2026)

- Data: 17/10/2026. Origem: BRU-1078 (Marketing, frente 3). Escopo: a frase (para quem, o quê, por quê), variações de hero/title, objeções, vocabulário, limites, medição, red team. Molde de rigor: `jurimetria/docs/marketing/mensagem-2026-10.md` (BRU-625).
- Como ler:
  - **[dado: fonte]**: veio de uma fonte que dá para abrir e conferir; a fonte vai junto.
  - **[hipótese]**: dedução nossa, a testar. Pode orientar texto, não vira número nem promessa.
- Material interno. As alternativas aparecem por categoria e **não são nomeadas em copy pública**; a copy só descreve o que o Kronos Hub faz.
- Fato central: **ainda não há usuário externo medido.** O repositório é público desde 15/10 só com README, roadmap e docs; o código chega com a publicação. O único "cliente" com palavra registrada é o dono (Bruno). Todo o resto do público-alvo é [hipótese]. Estrelas, forks e issues do GitHub **não foram lidos** nesta rodada (limite de API do GitHub estourado em 17/10, 13h39 UTC); ler antes de afirmar qualquer coisa sobre tração.

---

## 1. A frase

> **Para** quem já roda agentes de código (Claude Code) e quer comandá-los pelo celular, **o Kronos Hub** é o harness open source e auto-hospedado que **só dá a tarefa por pronta com evidência** (PR no ar, checagem passando, arquivo pedido), **em vez de** confiar no agente que diz "feito".

Versão curta (EN): *Coding agents that prove the work is done.*
Versão curta (PT): *Agentes de código que provam que o trabalho está pronto.*

Por que esta e não "fila de agentes" (a frase anterior do site, "Put your coding agents to work on a queue"):
- A fila descreve o mecanismo; qualquer script caseiro também tem fila. O que o dono repete é o resultado: pronto de verdade. [dado: ver 2, itens 1 a 4]
- "Prova" é o único eixo que o README já sustenta com regra concreta (contrato, recibo, segunda revisão, "está no ar?"). [dado: README e `docs/harness.md`]

### Quem
- **Hoje (único medido):** dono de 6 produtos que comanda 4 contas do Claude Code e o Codex como revisor, pelo WhatsApp/painel/Linear. [dado: README; `plano-kronos-oss`, 15/10]
- **Alvo:** dev solo ou time pequeno que já paga Claude Code, tem mais tarefas que atenção e quer acompanhar de fora do computador. [hipótese: nenhum usuário externo ouvido]

### O quê
Receber um pedido em linguagem comum, virar contrato (resultado, escopo, o que não muda, prova de pronto), passar por fila, contas, revisão por um segundo modelo, CI, merge, deploy e voltar um recibo. [dado: README, diagrama do fluxo]

### Por quê, em vez de quê (comparação interna, por categoria)

| Alternativa | O que ela resolve | Onde a fonte mostra a falha | Rótulo |
|---|---|---|---|
| Claude Code / Codex direto no terminal | Uma tarefa por vez, com você olhando | O dono precisou de fila, alarme e recibo ao escalar para 4 contas | [dado: 2.1 a 2.4] |
| Agentes de nuvem de terceiros (editor ou serviço hospedado) | Menos instalação | Não há na fonte comparação medida; a vantagem "auto-hospedado, seus segredos no seu servidor" é o que o README afirma | [hipótese] |
| Scripts caseiros (cron, hooks, filas soltas) | Controle total | O próprio hub nasceu assim e cada regra importante virou teste/hook depois de falhar duas vezes | [dado: README, "regra que importa vira código"] |

---

## 2. Palavras reais e dados (o que existe)

Palavras do dono, literais:
1. Pedido, 15/10: *"o que falta pro Kronos virar repo open source? Tem que ter campo pra criar projeto, cadastrar variáveis, conectar os agentes; tudo que a gente faz tem que estar ali pra pessoa ver."* [dado: `superorganic-hub/docs/plans/2026-10-15-kronos-open-source.md`, cabeçalho "Pedido"]
2. Sobre o artigo "Harness Engineering" (Lunar, 06/09), 15/10: *"é isso"*. Tese salva: *"falha de agente quase sempre é do ambiente, não do raciocínio. Prompt muda uma tentativa; harness muda todas."* [dado: biblioteca/receitas/prompts/harness-engineering-lunar.md]
3. Lacunas que o dono mediu em si mesmo em 14/10: tarefa fechada como Done sem entregar (BRU-667), tarefa parada 4 h sem alarme (BRU-589), tarefa consultando o CI de 10 em 10 minutos. [dado: mesma receita, "Lacunas medidas em 14/10"]
4. Regras de fala do dono: "WhatsApp sem spam (um resumo só)"; "o agente do WhatsApp obedece sem burocracia" (08/10). [dado: memória `decisoes-do-bruno`, linhas 6 e 16]

Dados medidos:
- **1.046 registros de lições** (falhas dos agentes classificadas, `docs/licoes/licoes.jsonl`, lido em 17/10). Maiores classes: `bug_de_logica` 218, `deploy_nao_subiu_comentado` 165, `teste_que_nao_prova` 100, `cota_estourada` 83, `laco_de_execucoes` 67, `tarefa_parada` 65, `main_vermelho` 65. [dado: licoes.jsonl; a classificação é feita pelo próprio hub, sem auditoria externa]
  - Leitura [hipótese]: "agente disse que subiu e não subiu" (165) e "teste que não prova" (100) sustentam o eixo "prova de pronto".
- Código do hub privado em 15/10: 82 módulos, 29,3 mil linhas, ~1.540 testes. [dado: plano `kronos-open-source`, seção 1]
- **PRs: o número do README não foi reproduzido.** O README e o site dizem 1.309 PRs mergeados de 7 a 14/10. Não achei registro dessa medição nos relatórios nem no plano. Contando nos clones locais (12 repositórios, commits com `(#N)` entre 07/10 00h e 14/10 24h, todas as refs) deu **744**. [dado: `git log` local de 17/10; é piso, porque exclui merges sem squash e repositórios não clonados]. A diferença (565) pode vir de repositórios fora de `~/repos`, de PRs não-squash ou de contagem por API; **não afirmar 1.309 em anúncio nem em manchete até alguém repetir a conta por `gh search prs` e guardar o comando.** O hero novo não usa o número.
- Estrelas, forks, issues e tráfego do repositório e do site: **não medidos** (ver topo).

Vocabulário do cliente (usar nestas palavras; todas vêm do dono ou do README):
- "pronto" / "evidência" / "recibo" ("receipt back to you"); "fila"; "contrato da tarefa"; "contas" (de agente); "harness"; "do celular" / "pelo WhatsApp"; "tarefa parada"; "no ar".
- Evitar: "autônomo", "IA que faz tudo", "10x", "substitui sua equipe". [hipótese: soam a promessa; o README fala de controle e prova]

---

## 3. Variações de hero/title

Regra: title com no máximo 60 caracteres, EN e PT-BR. Contagem feita no código (ver teste).

| # | Eixo | Title EN | Title PT | h1 |
|---|---|---|---|---|
| A (aplicada) | Prova de pronto | `Kronos Hub: coding agents that prove the work is done` | `Kronos Hub: agentes de código que provam que terminaram` | Coding agents that **prove** the work is done. / Agentes de código que **provam** que o trabalho está pronto. |
| B | Do celular | `Kronos Hub: run your coding agents from your phone` | `Kronos Hub: comande seus agentes de código do celular` | Run your coding agents from your phone, and get a receipt. |
| C | Harness, não agente | `Kronos Hub: the harness around your coding agents` | `Kronos Hub: o harness em volta dos seus agentes` | A prompt changes one attempt. A harness changes every attempt. |

Por que A: é o único eixo sustentado por falha medida (2.3 e lições) e por regra implementada (README). B é o ganho que o dono usa todo dia, mas "do celular" sozinho é fácil de copiar; testar como segunda variante. C fala de "harness", palavra que o dev solo talvez ainda não use [hipótese]; boa para quem já leu o artigo, ruim para quem chega frio.

---

## 4. Objeções (e resposta que a fonte sustenta)

1. **"Já uso o Claude Code no terminal, para quê mais uma camada?"** → Terminal serve para uma tarefa olhada. Com várias contas, o dono precisou de contrato, alarme de tarefa parada e recibo. [dado: 2.3, README] Se você roda uma tarefa por vez, talvez não precise. [hipótese: ponto de corte]
2. **"Auto-hospedar dá trabalho."** → O roadmap promete `docker compose up` + `kronos init` em 15 minutos, e o site traz os 5 comandos. **Ainda não foi medido em máquina de terceiro.** [dado: ROADMAP, plano; tempo real: não medido]
3. **"Custa quanto?"** → O software é AGPL-3.0, sem preço. Cada conta de agente continua com o plano de quem a usa. O custo por tarefa não está publicado. [dado: LICENSE; custo: não medido]
4. **"Segredo no servidor de um desconhecido?"** → Roda no seu servidor; o cofre é local, cifrado, e valor nunca volta à tela. [dado: ROADMAP, Vault; a revisão externa de segurança não existe]
5. **"AGPL assusta empresa."** → Usar e modificar internamente é livre; só quem oferece versão modificada como serviço precisa publicar as mudanças. [dado: README, "License"] Se isso trava times pequenos: [hipótese, não ouvido]

---

## 5. Limites (o que a copy não pode dizer)

- Não dizer "1.309 PRs" em título, anúncio ou post até reproduzir (seção 2). O site já traz a frase com o número na seção de prova; **sinalizado para o dono decidir** se tira ou confirma.
- Não dizer que "o agente nunca erra" ou "garantido": as lições (1.046) mostram falhas todos os dias; a promessa é detectar e provar, não zerar erro.
- Não dizer "pronto para instalar": o repositório público ainda recebe o código (status no próprio site).
- Não prometer taxa de acerto, economia de tempo ou de custo: nenhum número medido.
- Sem clipe, reel ou vídeo animado de divulgação (BRU-825, 15/10): material de lançamento é texto e README.
- Nome: "Kronos" é marca de software de RH de terceiro e `kronoshub.com` existe (plano 15/10, "O nome nunca é fixo"). Não depositar marca nem comprar mídia paga com o nome antes de decidir. [dado: plano]

## 6. Próximos passos de medição

1. Ler estrelas, forks, issues e visitantes (GitHub e GA4/Clarity do domínio) quando o limite da API liberar; escrever o resultado aqui, mesmo se for zero.
2. Repetir a contagem de PRs com `gh search prs --merged` e guardar o comando e a data; corrigir README e site para o número reproduzível.
3. Medir o tempo real de instalação (`git clone` até `/saude` ok) em uma VPS limpa, sem ajuda.
4. Conseguir 5 conversas com devs que usam Claude Code com mais de uma conta (voz real do cliente, que hoje não existe); perguntar o que fazem quando um agente diz "feito".
5. Teste A/B de title (A contra B) depois do primeiro tráfego orgânico; só com amostra que não seja robô.
6. Repetir a contagem de lições por classe a cada mês para ver se `deploy_nao_subiu_comentado` e `tarefa_parada` caem (é a prova de que o harness funciona, e vira prova pública no item 3 do roadmap).

## 7. Red team

1. **"Isso é um wrapper de prompts com fila; não tem moat."** → O ponto não é o modelo, é o ambiente: contrato, segundo revisor, recibo, alarme. Falha do agente é de ambiente (tese salva). Porém, o código ainda não está público para conferir; até lá a defesa é só texto. [dado: README; hipótese: valor percebido]
2. **"1.309 PRs em 7 dias é vaidade, quantidade sem qualidade."** → Ataque correto: o número não foi reproduzido (744 no mínimo local) e PR não mede valor. A resposta é não usar o número como argumento principal e mostrar o que o recibo prova (no ar, checagem passando) e as falhas registradas (1.046). [dado: seção 2]
3. **"Quem é o cliente? Só você usa."** → Verdade hoje: não há usuário externo medido. A resposta honesta é publicar isso e usar o primeiro lote de conversas (próximo passo 4) para decidir se o alvo é dev solo, time pequeno ou outro. [dado: topo do documento]
