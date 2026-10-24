"""GETs do hub: medição por consumidor, cache e reserva da cota.

Não grava URLs, corpos de erro ou credenciais na telemetria. Contadores são
requisições REST efetivas, separados de acertos do cache e pausas locais.
"""
import hashlib
import json
import os
import pwd
import sqlite3
import subprocess
import time

from hub import conta, incidentes, github_app


class CotaBaixa(RuntimeError):
    pass


class ErroHTTP(RuntimeError):
    def __init__(self, status):
        super().__init__(f'GitHub REST falhou (HTTP {status})')
        self.status = status


_ESQUEMA = '''
      CREATE TABLE IF NOT EXISTS cache (chave TEXT PRIMARY KEY, em REAL, etag TEXT, corpo TEXT);
      CREATE TABLE IF NOT EXISTS cota (ident TEXT PRIMARY KEY, restante INTEGER, reset REAL);
      CREATE TABLE IF NOT EXISTS cota_hist (hora INTEGER, ident TEXT, restante INTEGER, limite INTEGER);
      CREATE INDEX IF NOT EXISTS cota_hist_ident_hora ON cota_hist (ident, hora);
      CREATE TABLE IF NOT EXISTS credencial (ident TEXT PRIMARY KEY, rotulo TEXT);
      CREATE TABLE IF NOT EXISTS consumo (hora INTEGER, consumidor TEXT, ident TEXT,
        tipo TEXT, status INTEGER, quantidade INTEGER,
        PRIMARY KEY(hora, consumidor, ident, tipo, status));
    '''


def _banco_pasta(base_dados=None):
    pasta = conta.pasta(base_dados) / 'github-api'
    pasta.mkdir(parents=True, exist_ok=True, mode=0o700)
    return pasta


def _banco(base_dados=None):
    pasta = conta.pasta(base_dados) / 'github-api'
    pasta.mkdir(parents=True, exist_ok=True, mode=0o700)
    arq = pasta / 'consumo.sqlite3'
    fd = os.open(arq, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)  # root não segue link plantado na pasta do claude
    os.close(fd)
    con = sqlite3.connect(arq, timeout=10)
    _dono_claude(pasta)
    con.executescript(_ESQUEMA)
    _dono_claude(pasta)  # as tabelas/-wal/-shm já existem aqui
    return con


def _dono_claude(pasta):
    """Rotina root cria o banco; painel e agentes (claude) precisam abrir: devolve o dono. Melhor-esforço."""
    if os.geteuid() != 0:
        return
    try:
        pw = pwd.getpwnam('claude')
        # a pasta é do claude: nada de seguir link simbólico (escalada de privilégio via root)
        dfd = os.open(pasta, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.chown(dfd, pw.pw_uid, pw.pw_gid)
            for nome in os.listdir(dfd):
                if nome.startswith('consumo.sqlite3'):
                    os.chown(nome, pw.pw_uid, pw.pw_gid, dir_fd=dfd, follow_symlinks=False)
        finally:
            os.close(dfd)
    except (KeyError, OSError):
        pass


def _banco_ou_memoria(base_dados=None):
    """Banco real; se não abrir (PermissionError, sqlite), um banco em memória: a leitura segue sem medição."""
    try:
        return _banco(base_dados)
    except (sqlite3.Error, OSError):
        con = sqlite3.connect(':memory:')
        con.executescript(_ESQUEMA)
        return con


def _contar(con, agora, consumidor, ident, tipo, status=0):
    try:
        con.execute('''INSERT INTO consumo VALUES (?, ?, ?, ?, ?, 1)
          ON CONFLICT DO UPDATE SET quantidade=quantidade+1''',
                    (int(agora // 3600), consumidor, ident, tipo, status))
        con.commit()
    except sqlite3.Error:  # medição é melhor-esforço
        pass


contar = _contar


def registrar_http(consumidor, token, status, base_dados=None, agora=None):
    """Conta uma chamada à API feita fora do cliente (POST/PATCH por urllib); só consumidor, ident e status."""
    try:
        con = _banco(base_dados)
    except (sqlite3.Error, OSError):  # medição é melhor-esforço
        return
    try:
        _contar(con, time.time() if agora is None else agora, consumidor,
                ident(token), 'http', status)
    finally:
        con.close()


_AUTH = {}  # memo do `gh auth token` (local, sem rede): sucesso 300 s, falha 30 s
TTL_AUTH, TTL_AUTH_FALHA = 300, 30


def ident(token):
    """Identificador da credencial na telemetria: hash curto do token (nunca o token)."""
    return hashlib.sha256((token or '').encode()).hexdigest()[:16]


def resolver_token(token=None, rodar=None):
    """Token efetivo: o informado, a App, GH_TOKEN/GITHUB_TOKEN ou `gh auth token`. Vazio se nada resolver."""
    if not token:
        try:
            token = github_app.token()
        except Exception:  # App mal configurada: cai para o ambiente
            token = None
    token = token or os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    agora = time.time()
    if not token and _AUTH.get('ate', 0) > agora:  # memo com TTL: sem `gh auth token` a cada chamada
        return _AUTH['token']
    if not token:
        try:
            token = (rodar or subprocess.run)(['gh', 'auth', 'token'], capture_output=True, text=True,
                                              check=True, timeout=15).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            token = ''
        _AUTH.update(token=token, ate=agora + (TTL_AUTH if token else TTL_AUTH_FALHA))
    return token or ''


def _incidente_cota(ident_, restante, reset, base_dados=None):
    if restante < 500:
        incidentes.registrar(f'GitHub: {restante} chamadas restantes; reset {int(reset)}. Consumidores não urgentes pausados.',
                             'cota_estourada', 'hub/github_api.py', issue='KRN-1084',
                             chave=f'github-cota:{ident_}:{int(reset)}', base_dados=base_dados)


def _credenciais(rodar=None):
    """[(rótulo, token)] conhecidos, sem repetir: App e o token do gh/ambiente."""
    achadas = []
    try:
        app = github_app.token()
    except Exception:  # App mal configurada não derruba a amostra do gh
        app = None
    if app:
        achadas.append(('App', app))
    env = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    try:
        from hub import repos
        hub_tok = repos.token_github()  # token real de dados_diarios, vigia_deploy e wiki.sh
    except Exception:
        hub_tok = None
    if not env and not hub_tok:
        try:
            env = (rodar or subprocess.run)(['gh', 'auth', 'token'], capture_output=True, text=True,
                                            check=True, timeout=15).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            env = ''
    vistos = {app}
    for rotulo, tok in (('gh pessoal', env), ('gh hub', hub_tok)):
        if tok and tok not in vistos:
            vistos.add(tok)
            achadas.append((rotulo, tok))
    return achadas


def _com_tentativas(fn, vezes=4, espera=3):
    """Os consumidores de gh gravam no mesmo banco; escritor concorrente segura o lock além do timeout (cron github-cota 1×)."""
    for i in range(vezes):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if 'locked' not in str(e) or i == vezes - 1:
                raise
            time.sleep(espera)


def _banco_com_tentativas(base_dados=None):
    return _com_tentativas(lambda: _banco(base_dados))


def amostrar_cota(base_dados=None, rodar=None, agora=None):
    """Lê GET /rate_limit (não conta no limite) por credencial; grava cota, cota_hist e incidente <500."""
    agora = time.time() if agora is None else agora
    rodar = rodar or subprocess.run
    feitas = []
    con = _banco_com_tentativas(base_dados)
    try:
        for rotulo, token in _credenciais(rodar):
            ident_ = ident(token)
            try:
                r = rodar(['gh', 'api', 'rate_limit'], capture_output=True, text=True, encoding='utf-8',
                          timeout=30, env={**os.environ, 'GH_TOKEN': token})
                core = json.loads(r.stdout)['resources']['core']
                restante, limite, reset = int(core['remaining']), int(core['limit']), float(core['reset'])
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
                continue  # sem texto de erro: pode conter dados privados
            _com_tentativas(lambda: (
                con.execute('INSERT OR REPLACE INTO cota VALUES (?, ?, ?)', (ident_, restante, reset)),
                con.execute('INSERT OR REPLACE INTO credencial VALUES (?, ?)', (ident_, rotulo)),
                con.execute('INSERT INTO cota_hist VALUES (?, ?, ?, ?)', (int(agora), ident_, restante, limite)),
                con.commit()))
            _incidente_cota(ident_, restante, reset, base_dados)
            feitas.append((rotulo, ident_, restante, limite))
        con.execute('DELETE FROM cota_hist WHERE hora < ?', (int(agora) - 8*86400,))
        # token da App expira em ~1 h e o ident é hash do token: poda credenciais vencidas há mais de 2 h
        con.execute('DELETE FROM cota WHERE reset < ?', (agora - 7200,))
        con.execute('DELETE FROM credencial WHERE ident NOT IN (SELECT ident FROM cota)')
        con.commit()
    finally:
        con.close()
    return feitas


def cotas(base_dados=None, agora=None):
    """[(ident, rótulo, restante, limite, reset)] das credenciais amostradas nas últimas 2 h."""
    agora = time.time() if agora is None else agora
    try:
        con = _banco(base_dados)
    except (sqlite3.Error, OSError):
        return []
    try:
        return con.execute('''SELECT c.ident, COALESCE(k.rotulo, ''), c.restante,
            (SELECT limite FROM cota_hist h WHERE h.ident=c.ident ORDER BY hora DESC LIMIT 1), c.reset
            FROM cota c LEFT JOIN credencial k USING(ident)
            WHERE EXISTS (SELECT 1 FROM cota_hist h WHERE h.ident=c.ident AND h.hora >= ?)
            ORDER BY k.rotulo, c.ident''', (int(agora) - 7200,)).fetchall()
    finally:
        con.close()


def get(caminho, consumidor='esperas', urgente=False, base_dados=None, agora=None, rodar=None, token=None, usar_cache=True):
    """GET REST JSON; cache compartilhado de 60 s por credencial e endpoint.

    `token` permite a integração com o provedor da App; ausente, mantém a
    autenticação do gh. Não intercepta comandos gh externos a este cliente.
    """
    if caminho.startswith(('http:', 'https:', '//')) or not caminho.startswith('repos/'):
        raise ValueError('somente endpoints relativos repos/ são permitidos')
    agora = time.time() if agora is None else agora
    rodar = rodar or subprocess.run
    token = resolver_token(token, rodar)
    if not token:
        raise RuntimeError('GitHub sem credencial')
    ident_ = ident(token)
    chave = hashlib.sha256((ident_ + '\n' + caminho).encode()).hexdigest()
    con = _banco_ou_memoria(base_dados)
    try:
        cache = con.execute('SELECT em, etag, corpo FROM cache WHERE chave=?', (chave,)).fetchone()
        if usar_cache and cache and 0 <= agora-cache[0] < 60:
            _contar(con, agora, consumidor, ident_, 'cache')
            return json.loads(cache[2])
        cota = con.execute('SELECT restante, reset FROM cota WHERE ident=?', (ident_,)).fetchone()
        if cota and cota[0] < 500 and agora < cota[1] and not urgente:
            _contar(con, agora, consumidor, ident_, 'pausa')
            raise CotaBaixa('GitHub: consumidor pausado até o reset da cota')
        args = ['gh', 'api', '--include', '--method', 'GET', caminho]
        if cache and cache[1]:
            args += ['-H', 'If-None-Match: ' + cache[1]]
        env = {**os.environ, 'GH_TOKEN': token}
        r = rodar(args, capture_output=True, text=True, encoding='utf-8', timeout=60, env=env)
        # gh normaliza as linhas em text=True; aceitar CRLF também ajuda os testes.
        cab, sep, corpo = r.stdout.replace('\r\n', '\n').partition('\n\n')
        linhas = cab.splitlines()
        status = int(linhas[0].split()[1]) if linhas and linhas[0].startswith('HTTP/') else 0
        headers = dict(l.split(':', 1) for l in linhas[1:] if ':' in l)
        headers = {k.lower(): v.strip() for k, v in headers.items()}
        _contar(con, agora, consumidor, ident_, 'http', status)
        if 'x-ratelimit-remaining' in headers and 'x-ratelimit-reset' in headers:
            restante, reset = int(headers['x-ratelimit-remaining']), float(headers['x-ratelimit-reset'])
            con.execute('INSERT OR REPLACE INTO cota VALUES (?, ?, ?)', (ident_, restante, reset))
            con.commit()
            _incidente_cota(ident_, restante, reset, base_dados)
        if status == 304 and cache:
            corpo = cache[2]
        elif r.returncode or not sep or not 200 <= status < 300:
            # Não propagar stderr: pode conter URLs/dados privados.
            raise ErroHTTP(status)
        valor = json.loads(corpo)
        try:
            con.execute('INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?)',
                        (chave, agora, headers.get('etag', cache[1] if status == 304 and cache else ''), corpo))
            # Limita retenção sem apagar a amostra corrente; cache antigo não é mais útil.
            con.execute('DELETE FROM cache WHERE em < ?', (agora-86400,))
            con.execute('DELETE FROM consumo WHERE hora < ?', (int(agora//3600)-24*8,))
            con.commit()
        except sqlite3.Error:  # cache é melhor-esforço: banco corrompido não pode derrubar a leitura já feita
            pass
        return valor
    finally:
        con.close()


def resumo(base_dados=None, agora=None):
    agora = time.time() if agora is None else agora
    try:
        con = _banco(base_dados)
    except (sqlite3.Error, OSError):
        return []
    try:
        return con.execute('''SELECT consumidor, tipo, status, SUM(quantidade) FROM consumo
          WHERE hora >= ? GROUP BY consumidor, tipo, status ORDER BY consumidor, tipo, status''',
                           (int(agora//3600)-23,)).fetchall()
    finally:
        con.close()


TIPOS = ('http', 'cache', 'pausa', 'gh')


def pivo(linhas):
    """[(consumidor, tipo, status, n)] -> [(consumidor, http, cache, pausa, gh, total)] por total decrescente."""
    por = {}
    for consumidor, tipo, _status, n in linhas:
        d = por.setdefault(consumidor, dict.fromkeys(TIPOS, 0))
        if tipo in d:
            d[tipo] += n
    saida = [(c, *(d[t] for t in TIPOS), sum(d.values())) for c, d in por.items()]
    return sorted(saida, key=lambda x: (-x[-1], x[0]))


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['amostrar']:
        feitas = amostrar_cota()
        marca = _banco_pasta() / 'sem-credencial.dia'
        if not feitas:
            hoje = time.strftime('%Y-%m-%d', time.gmtime())
            repetida = marca.exists() and marca.read_text() == hoje
            if not repetida:  # só a 1ª falha do dia sai ≠ 0 (rotina.sh grava falhas.jsonl a cada código ≠ 0)
                marca.write_text(hoje)
                _dono_claude(marca.parent)
                sys.exit('github_api amostrar: nenhuma credencial amostrada')
            print('github_api amostrar: nenhuma credencial amostrada (falha já registrada hoje)', file=sys.stderr)
            sys.exit(0)
        marca.unlink(missing_ok=True)
        for rotulo, ident_, restante, limite in feitas:
            print(f'{rotulo} {ident_}: {restante}/{limite}')
    else:
        sys.exit('uso: python3 -m hub.github_api amostrar')
