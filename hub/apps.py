"""Acesso só leitura aos apps: ambiente do contêiner (docker inspect, local ou por ssh), psql só leitura no contêiner
do banco, PostgREST paginado, Google Meu Negócio e GET de JSON.
A chave fica só em memória: nada aqui grava segredo em disco nem em log."""
import json
import shlex
import subprocess
import urllib.parse
import urllib.request


def _rodar(ssh, *cmd, entrada=None):
    cmd = [*ssh, shlex.join(cmd)] if ssh else list(cmd)  # o ssh junta os argumentos num shell remoto
    try:
        return subprocess.run(cmd, input=entrada, capture_output=True, text=True, check=True, timeout=300).stdout
    except subprocess.CalledProcessError as e:  # stderr sem segredo; nunca a entrada (SQL) nem o ambiente
        raise RuntimeError(f"{cmd[0]} falhou ({e.returncode}): {(e.stderr or '')[-200:].strip()}") from e


def _conteiner(prefixo, ssh):
    """Nome exato se existir (ex.: clienteh-db), senão o primeiro que começa com `prefixo` (Swarm/Dokploy)."""
    nomes = _rodar(ssh, "docker", "ps", "--format", "{{.Names}}").split()
    nome = prefixo if prefixo in nomes else next((n for n in nomes if n.startswith(prefixo)), None)
    if not nome:
        raise SystemExit(f"contêiner {prefixo}* não está rodando")
    return nome


def env_do_conteiner(prefixo, ssh=None):
    """Variáveis do contêiner `prefixo`. `ssh` = comando ssh (lista) para outra VPS."""
    linhas = _rodar(ssh, "docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}",
                    _conteiner(prefixo, ssh))
    return dict(l.split("=", 1) for l in linhas.splitlines() if "=" in l)


def psql_json(prefixo, sql, ssh=None, psql="psql -U ${POSTGRES_USER:-postgres} -d ${POSTGRES_DB:-postgres}"):
    """SELECT em transação só leitura no Postgres do contêiner; devolve as linhas como lista de dicts."""
    saida = _rodar(ssh, "docker", "exec", "-i", _conteiner(prefixo, ssh), "sh", "-c", psql + " -Atq -v ON_ERROR_STOP=1",
                   entrada=f"set default_transaction_read_only = on;\nselect coalesce(json_agg(t), '[]') from ({sql}) t;")
    return json.loads(saida)


def http_json(url, headers=None, data=None):
    req = urllib.request.Request(url, headers={"User-Agent": "nimbus-hub/leitura", **(headers or {})}, data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


class Rest:
    """PostgREST só leitura, com paginação."""

    def __init__(self, url, chave):
        self.base = url.rstrip("/") + "/rest/v1/"
        self.h = {"apikey": chave, "Authorization": f"Bearer {chave}"}

    def get(self, tabela, limite=20000, **params):
        out, passo = [], 1000
        while len(out) < limite:
            q = urllib.parse.urlencode({**params, "limit": min(passo, limite - len(out)), "offset": len(out)}, safe=",.()*:")
            lote = http_json(f"{self.base}{tabela}?{q}", self.h)
            out += lote
            if len(lote) < passo:
                break
        return out


def token_gmb(env):
    """Token OAuth do Google Meu Negócio do app da fsite05 (refresh token do contêiner do site), só em memória."""
    return http_json("https://oauth2.googleapis.com/token", data=urllib.parse.urlencode({
        "client_id": env["GOOGLE_CLIENT_ID"], "client_secret": env["GOOGLE_CLIENT_SECRET"],
        "refresh_token": env["GOOGLE_REFRESH_TOKEN"], "grant_type": "refresh_token"}).encode())["access_token"]


def unidades_gmb(env):
    """Id do local de cada unidade da fsite05 na API do Business Profile."""
    return {"alfa": env.get("GMB_LOCATION_ALFA", "05572275004056503923"),
            "beta": env.get("GMB_LOCATION_BETA", "15631233265849930257")}


def avaliacoes_google(env, tok=None):
    """Últimas 50 avaliações, média e total do Google Meu Negócio por unidade da fsite05 (env do contêiner do site)."""
    tok = tok or token_gmb(env)
    return {unidade: http_json(f"https://mybusiness.googleapis.com/v4/accounts/{env['GMB_ACCOUNT_ID']}/locations/{loc}"
                               "/reviews?pageSize=50&orderBy=updateTime%20desc", {"Authorization": f"Bearer {tok}"})
            for unidade, loc in unidades_gmb(env).items()}
