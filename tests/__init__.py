import os
import json
import tempfile

# identidade de mentira: nada de teste depende do toml nem do legado da VPS (config.LEGADO_VALOR). Os dados do dono não estão
# no repo (1.6): sites/tarefas/hub.json vêm de exemplos/ ou de fixtures. O GitHub segue na org example-org para os testes de repo/recibo.
# dados do dono dos testes: cadastro e hub.json fictícios em tests/fixtures/dados; tarefas = exemplos/tarefas (padrão neutro)
_DADOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "dados")
os.environ["KRONOS_PROJETOS"] = os.path.join(_DADOS, "projetos")
os.environ["KRONOS_HUB_JSON"] = os.path.join(_DADOS, "hub.json")
os.environ["KRONOS_CONFIG"] = "/nao/existe/kronos.toml"
os.environ["KRONOS_DONO_NOME"] = "Ana"
os.environ["KRONOS_GITHUB_ORG"] = "example-org"
os.environ["KRONOS_WHATSAPP_REMETENTES"] = '{"fsite05": "crm", "asite01": "numero"}'

# Configuração explícita dos testes: o BI e as mensagens não dependem do host.
os.environ["KRONOS_BI_NEGOCIOS"] = json.dumps({"asite01": "asite01", "clientej": "ClienteJ", "fsite05": "Cliente A", "clientei": "ClienteI", "psite13": "Cliente G", "clientee": "ClienteE"})
os.environ["KRONOS_PRODUTO_NOME"] = "Pereiraos"
os.environ["KRONOS_PLUGINS_LIGADOS"] = '["*"]'  # os plugins de privado/plugins ligados (as telas deles são testadas aqui)

# testes nunca gravam no custos.jsonl de verdade do DataForSEO (hub/dataforseo.py); o custo de mentira que somam não
# pode bater no teto diário de outro teste (o teste do teto põe o próprio)
os.environ["DATAFORSEO_PASTA"] = tempfile.mkdtemp(prefix="dfs-testes-")
os.environ["DATAFORSEO_TETO_DIA_USD"] = "1000000"

# testes nunca gravam nos dados de verdade do hub: sem isso, data() cai no /var/lib/nimbus/reports da VPS e o
# teste da agenda punha "· Arrumar rodapé" com falha na fila do resumo do WhatsApp (o Fulano recebia, 14/10/2026)
os.environ["HUB_DATA"] = tempfile.mkdtemp(prefix="hub-testes-")

# as caixas do bot (avisos ao Fulano, respostas do WhatsApp, fila do agente, pedidos) também são produção:
# um teste que esquece o mock não pode mandar nada ao Fulano nem ler pedido de verdade
_bot = tempfile.mkdtemp(prefix="bot-testes-")
for _v, _sub in (("CAIXA_AVISOS", "avisos"), ("LINEAR_ENTRADA_ZAP", "linear-entrada"), ("PEDIDOS_DIR", "agente"),
                 ("CHAVE_APROVACAO", "root-fila/.chave-aprovacao"), ("PEDIDOS_BOT", "pedidos.json"),
                 ("PARCEIROS_LOG", "agente/parceiros.jsonl"), ("FILA_AGENTE", "agente/fila"),
                 ("FEITOS_AGENTE", "agente/feitos"), ("ROOT_FILA", "root-fila"), ("CAIXA_COMANDOS", "comandos"),
                 ("PROSPECCAO_ESTADO", "prospeccao/estado.json"), ("ASITE01_WPP_DATA", ""), ("BOT_DATA", "")):
    os.environ[_v] = os.path.join(_bot, _sub)
os.makedirs(os.path.join(_bot, "avisos"), exist_ok=True)

# as chaves das reservas de API (hub.json → reservas_api) existem de verdade na VPS: teste não pode ver a do João
# e mandar Haiku para a reserva; o arquivo que não existe vale uma lista de uma reserva sem chave. Teste de reserva põe o seu.
os.environ["ANTHROPIC_API_KEY_FILE"] = os.path.join(tempfile.mkdtemp(prefix="chave-testes-"), "sem-chave")

# testes não herdam a conta nem a tarefa do processo que os roda (CLAUDE_CONTA=2 quebrava pausa.json; TAREFA_NOME
# de uma sessão do hub carimbava os avisos dos testes)
for _v in ("CLAUDE_CONTA", "TAREFA_NOME", "TAREFA_LOG"):
    os.environ.pop(_v, None)

# testes nunca falam com o Linear de verdade: sem a chave de /etc/nimbus/linear.env e com a API numa porta fechada,
# a tela que lê o Linear cai na hora no "Linear indisponível" (antes, ~90 chamadas reais de até 30 s; como root,
# o atualizar-hub passava de 15 min). Teste que precisa do Linear faz o próprio mock.
for _v in ("LINEAR_API_KEY", "LINEAR_APP_CLIENT_ID", "LINEAR_APP_CLIENT_SECRET"):
    os.environ.pop(_v, None)
from hub import linear as _linear  # noqa: E402

_linear.ARQ_CHAVE = _linear.Path("/nao-existe/linear.env")
_linear.API = "http://127.0.0.1:9/graphql"
