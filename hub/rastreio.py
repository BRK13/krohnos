"""Rastreio da execução do agente (F8): cada passo do stream (fala, pensamento, ferramenta com entrada e saída)
vira uma linha de `passo` em $HUB_DATA/claude/rastreio.db, ligada à execução pelo mesmo id da telemetria (F2).

Segredo nunca entra: entrada, saída e fala passam por `limpar` (chaves da `conta.chaves_api()`, os tokens da telemetria e
os padrões de saída bruta de ferramenta: atribuição `*_KEY=`, URL com senha, PEM, Authorization, sk_live_/re_/xox?-) antes de
gravar, e a assinatura do `thinking` nunca é lida. Texto de entrada/saída é truncado em 4000 caracteres; o tamanho
original da saída fica em `n_bytes_saida`. `duracao_s` só existe quando o stream traz `timestamp` (nunca inventado).
"""
import contextlib
import datetime as dt
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

from hub import conta, telemetria

LIMITE = 4000
# Profundidade máxima de aninhamento lida da entrada/saída: o excedente vira "•••" (nunca estoura a recursão do Python)
PROFUNDIDADE = 50
RETENCAO_DIAS = 30  # passos de execução mais velha que isso saem do banco (indexar_novos)
# Mesmo padrão de token da telemetria; a chave nossa sai por conta.chaves_api()
RE_SEGREDO = telemetria.RE_SEGREDO
# Saída bruta de ferramenta (`env`, `cat .env`, curl -v): o nome fica, o valor vira "•••". Em ordem: o bloco PEM inteiro
# antes de tudo (tem `=` e `:` dentro), a senha da URL, o cabeçalho, as atribuições (_atribuicoes) e os tokens soltos.
# Sem quantificador sobreposto: cada padrão começa num literal ou num início de palavra e anda com classe limitada ou
# possessiva (tempo linear; `limpar("key" * 33333)` levava 103 s com `[\w.-]*` dos dois lados da palavra-chave).
FILTROS = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----|\Z)", re.S),
     "-----BEGIN PRIVATE KEY----- ••• -----END PRIVATE KEY-----"),
    (re.compile(r"(://[^\s:/@'\"]++:)[^\s@/'\"]++@"), r"\1•••@"),
    (re.compile(r"(?i)(\bauthorization[\"']?[ \t]*+[:=][ \t]*+[\"']?)(?:(?:basic|bearer|token|digest)[ \t]++)?[^\s\"',]++"),
     r"\1•••"),
)
RE_TOKENS = re.compile(r"\b[sr]k_(?:live|test)_\w++|\bre_(?=[A-Za-z0-9_]{0,200}\d)[A-Za-z0-9_]{10,}+|\bxox[abprs]-[\w-]++")
# nome de variável inteiro (possessivo, só no início de palavra) e o separador; a palavra-chave é conferida à parte
RE_NOME = re.compile(r"(?<![\w.-])([\w.-]++)[\"']?[ \t]*+[=:][ \t]*+")
RE_CHAVE = re.compile(r"(?i)pass|secret|token|key|senha|credential")
RE_VALOR = re.compile(r"\"(?:[^\"\\\n]|\\.){0,512}+\"|'(?:[^'\\\n]|\\.){0,512}+'|[^\s,;}\]]++")
TETO_LIMPAR = 2 * LIMITE  # `limpar` lê no máximo isto; o resto nunca é gravado (o gravado é cortado em LIMITE)


def _atribuicoes(t):
    """`NOME_COM_KEY=valor` → `NOME_COM_KEY=•••` (aspas e `"nome": "v"` de JSON também). Linear: cada nome é lido uma
    vez; nome sem palavra-chave só consome o nome e o separador (o valor ainda é olhado como nome)."""
    out, i = [], 0
    for m in RE_NOME.finditer(t):
        if m.start() < i or not RE_CHAVE.search(m.group(1)):
            continue
        v = RE_VALOR.match(t, m.end())
        if v:
            out += [t[i:m.end()], "•••"]
            i = v.end()
    return "".join(out) + t[i:]


BORDA = 1024  # o pedaço da borda descartado quando a última palavra da janela é maior que isso


def _janela(texto):
    """Até TETO_LIMPAR caracteres. Cortado: a última palavra (talvez metade de um segredo) vira "•••" antes de limpar
    (palavra maior que BORDA: só os últimos BORDA caracteres), para nenhum pedaço de segredo da borda sobrar nem
    escorregar para dentro do que é gravado."""
    if len(texto) <= TETO_LIMPAR:
        return texto
    w = texto[:TETO_LIMPAR]
    fim = max(w.rfind(" "), w.rfind("\n"), w.rfind("\t"))
    return (w[:fim + 1] if TETO_LIMPAR - fim - 1 <= BORDA else w[:-BORDA]) + "•••"


def caminho(base_dados=None) -> Path:
    return conta.pasta(base_dados) / "rastreio.db"


def conectar(base_dados=None):
    arq = caminho(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    # a agenda e o cron escrevem ao mesmo tempo: WAL e espera de 10 s
    con = sqlite3.connect(arq, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
      CREATE TABLE IF NOT EXISTS passo (
        execucao_id TEXT NOT NULL, n INTEGER NOT NULL, tipo, ferramenta, tool_use_id, entrada, saida,
        erro INTEGER, inicio, duracao_s REAL, texto, n_bytes_saida INTEGER, PRIMARY KEY (execucao_id, n));
      CREATE TABLE IF NOT EXISTS indexado (execucao_id TEXT PRIMARY KEY, stream, tamanho INTEGER, em);
    """)
    _migrar(con)
    return con


# colunas do cabeçalho da execução (vêm do uso.jsonl): ADD COLUMN idempotente em banco já criado pela Task 1
COLUNAS_EXEC = (("nome", "TEXT"), ("issue", "TEXT"), ("inicio", "TEXT"), ("conta", "TEXT"), ("modelo", "TEXT"),
                ("ok", "INTEGER"))


def _migrar(con):
    tem = {r[1] for r in con.execute("PRAGMA table_info(indexado)")}
    for col, tipo in COLUNAS_EXEC:
        if col not in tem:
            try:
                con.execute(f"ALTER TABLE indexado ADD COLUMN {col} {tipo}")
            except sqlite3.OperationalError:  # outro processo migrou entre o PRAGMA e o ALTER
                pass
    con.commit()


def execucao_id(reg: dict) -> str:
    """Id da execução: o da telemetria (F2), `telemetria.id_de`."""
    return telemetria.id_de(reg)


def limpar(texto, chaves=None):
    if texto is None:
        return None
    texto = _janela(str(texto))
    for k in conta.chaves_api() if chaves is None else chaves:
        if k:
            texto = texto.replace(k, "•••")
    for padrao, troca in FILTROS:
        texto = padrao.sub(troca, texto)
    texto = RE_TOKENS.sub("•••", _atribuicoes(texto))
    return RE_SEGREDO.sub("•••", texto)


def _podar(v, prof=0):
    """Corta o aninhamento além de PROFUNDIDADE (o excedente vira "•••"); recursão limitada, nunca RecursionError."""
    if isinstance(v, (dict, list)) and prof >= PROFUNDIDADE:
        return "•••"
    if isinstance(v, dict):
        return {k: _podar(x, prof + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [_podar(x, prof + 1) for x in v]
    return v


def _texto(v):
    """Entrada (dict) ou saída (str ou lista de blocos) do stream -> texto (aninhamento podado em PROFUNDIDADE)."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    v = _podar(v)
    if isinstance(v, list):
        partes = [b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else
                  (b if isinstance(b, str) else json.dumps(b, ensure_ascii=False)) for b in v]
        return "\n".join(str(x) for x in partes)
    return json.dumps(v, ensure_ascii=False)


def _limpar_valores(v, chaves, prof=0):
    """Limpa cada texto de dentro da entrada (valores e chaves) antes do json.dumps: o escape de aspas não esconde segredo.
    Aninhamento além de PROFUNDIDADE vira "•••" (descartado, nunca gravado sem limpar)."""
    if isinstance(v, str):
        return limpar(v, chaves)
    if isinstance(v, (dict, list)) and prof >= PROFUNDIDADE:
        return "•••"
    if isinstance(v, dict):
        return {limpar(k, chaves): _limpar_valores(x, chaves, prof + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [_limpar_valores(x, chaves, prof + 1) for x in v]
    return v


def _str(v):
    return v if isinstance(v, str) else None


def _inserir(con, eid, e, chaves):
    bruta = _texto(e.get("saida"))
    saida = limpar(bruta, chaves)
    entrada = limpar(_texto(_limpar_valores(e.get("entrada"), chaves)), chaves)
    texto = limpar(_texto(e.get("texto")), chaves)
    n_bytes = e.get("n_bytes_saida")
    if n_bytes is None and bruta is not None:
        n_bytes = len(bruta.encode("utf-8"))  # tamanho ORIGINAL, antes de limpar e truncar
    erro = e.get("erro")
    con.execute(
        "INSERT OR REPLACE INTO passo (execucao_id, n, tipo, ferramenta, tool_use_id, entrada, saida, erro, inicio, "
        "duracao_s, texto, n_bytes_saida) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, int(e["n"]), e.get("tipo"), _str(e.get("ferramenta")), _str(e.get("tool_use_id")),
         entrada[:LIMITE] if entrada is not None else None, saida[:LIMITE] if saida is not None else None,
         None if erro is None else int(bool(erro)), e.get("inicio"), e.get("duracao_s"),
         texto[:LIMITE] if texto is not None else None, n_bytes))


def gravar(execucao_id: str, evento: dict, base_dados=None) -> None:
    con = conectar(base_dados)
    try:
        with con:
            _inserir(con, execucao_id, evento, conta.chaves_api())
    finally:
        con.close()


def _ts(s):
    if not isinstance(s, str):
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)  # naive vs aware não estoura a subtração


def _eventos(arq):
    """Lê o stream linha a linha; linha inválida ou truncada é pulada, o resto segue."""
    with open(arq, "rb") as f:
        for bruta in f:
            try:
                ev = json.loads(bruta.decode("utf-8", "replace"))
            except (ValueError, RecursionError):  # linha inválida ou aninhada demais: pula, as demais seguem
                continue
            if isinstance(ev, dict):
                yield ev


def _passos(arq):
    passos, pendentes = [], {}
    for ev in _eventos(arq):
        msg = ev.get("message")
        blocos = msg.get("content") if isinstance(msg, dict) else None
        if ev.get("type") not in ("assistant", "user") or not isinstance(blocos, list):
            continue
        ts = _str(ev.get("timestamp"))
        for b in blocos:
            if not isinstance(b, dict):
                continue
            try:
                _bloco(ev["type"], b, ts, passos, pendentes)
            except RecursionError:  # bloco aninhado demais: descarta só ele, os demais seguem
                continue
    for n, p in enumerate(passos, 1):
        p["n"] = n
    return passos


def _bloco(tipo_ev, b, ts, passos, pendentes):
    """Um bloco do evento -> passo novo (ou saída do passo pendente). Texto calculado antes de mexer nas listas:
    se estourar, nada fica pela metade."""
    t = b.get("type")
    ident = _str(b.get("id"))  # id que não é texto (lista, dict) é ignorado
    if tipo_ev == "assistant" and t == "text":
        passos.append({"tipo": "fala", "texto": _texto(b.get("text")) or "", "inicio": ts})
    elif tipo_ev == "assistant" and t == "thinking":
        # só o tamanho: o stream real traz o conteúdo vazio e a assinatura, que nunca é gravada
        passos.append({"tipo": "pensamento", "n_bytes_saida": len(_texto(b.get("thinking") or "").encode("utf-8")), "inicio": ts})
    elif tipo_ev == "assistant" and t == "tool_use":
        p = {"tipo": "ferramenta", "ferramenta": _str(b.get("name")), "tool_use_id": ident,
             "entrada": b.get("input"), "inicio": ts}
        passos.append(p)
        if ident:
            pendentes[ident] = p
    elif tipo_ev == "user" and t == "tool_result":
        saida = _texto(b.get("content")) or ""
        uso = _str(b.get("tool_use_id"))
        p = pendentes.pop(uso, None) if uso else None
        if p is None:  # resultado sem tool_use: vira passo próprio, não se perde
            p = {"tipo": "resultado", "tool_use_id": uso, "inicio": ts}
            passos.append(p)
        else:
            a, z = _ts(p.get("inicio")), _ts(ts)
            if a and z:
                p["duracao_s"] = round((z - a).total_seconds(), 3)
        p["saida"] = saida
        p["erro"] = bool(b.get("is_error"))


def _arq_stream(reg):
    saida = str(reg.get("saida") or "")
    if not saida:
        return None
    # mesmo nome do produtor (agenda.py): o .md de saída com o sufixo trocado
    return Path(saida) if saida.endswith(".stream.jsonl") else Path(saida).with_suffix(".stream.jsonl")


def issue_de(nome):
    """`linear-` (nome da tarefa) -> ``; outro nome -> None."""
    m = re.fullmatch(r"linear-krn-(\d+)", str(nome or ""), re.I)
    return f"KRN-{int(m.group(1))}" if m else None


def _marcar(con, reg, eid, arq, tamanho):
    ok = reg.get("ok")
    con.execute(
        "INSERT OR REPLACE INTO indexado (execucao_id, stream, tamanho, em, nome, issue, inicio, conta, modelo, ok) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (eid, str(arq), tamanho, conta.iso(conta.agora()), _str(reg.get("nome")), issue_de(reg.get("nome")),
         _str(reg.get("inicio")), _str(reg.get("conta")), _str(reg.get("modelo")), None if ok is None else int(bool(ok))))


CODEX_VAZIO = "Execução do Codex: o stream não tem passos de ferramenta no formato do Claude."
CODEX_SEM_SAIDA = "Execução do Codex: cada ferramenta mostra só o comando (o stream do Codex não traz a saída)."


def eh_codex(r):
    """Registro do uso.jsonl ou linha de `indexado` de uma execução do Codex (conta "codex")."""
    return str((r or {}).get("conta") or "") == "codex" or (r or {}).get("executor") == "codex"


def _sem_eco_codex(passos):
    """O stream do Codex (agenda.evento_codex) grava o mesmo tool_use no item.started e no item.completed, sem id nem
    resultado: o par seguido e idêntico vira um passo só (repetição de verdade, o 3º igual, fica)."""
    saida = []
    for p in passos:
        ant = saida[-1] if saida else None
        if (ant and p["tipo"] == "ferramenta" and ant["tipo"] == "ferramenta" and not ant.get("_par")
                and (ant.get("ferramenta"), ant.get("entrada")) == (p.get("ferramenta"), p.get("entrada"))):
            ant["_par"] = True
            continue
        saida.append(p)
    return saida


def indexar_stream(reg: dict, base_dados=None) -> int:
    """Lê o `.stream.jsonl` da execução e substitui os passos dela; devolve o nº de passos GRAVADOS (0 se não há stream).
    Passo descartado (aninhamento que ainda estoure a recursão) não deixa buraco: `n` é contíguo entre os gravados."""
    arq = _arq_stream(reg)
    if arq is None:
        return 0
    try:
        tamanho = arq.stat().st_size
        passos = _passos(arq)
        if eh_codex(reg):
            passos = _sem_eco_codex(passos)
    except (OSError, TypeError, ValueError, AttributeError, RecursionError):  # stream malformado nunca estoura o chamador
        return 0
    eid, chaves = execucao_id(reg), conta.chaves_api()
    con = conectar(base_dados)
    try:
        with con:
            con.execute("DELETE FROM passo WHERE execucao_id=?", (eid,))
            gravados = 0
            for p in passos:
                p["n"] = gravados + 1  # renumera: descarte não deixa buraco
                try:
                    _inserir(con, eid, p, chaves)
                    gravados += 1
                except RecursionError:  # defesa: descarta só este passo (nunca grava sem limpar)
                    continue
            _marcar(con, reg, eid, arq, tamanho)
    finally:
        con.close()
    return gravados


# ---- leitura ----

RE_HEX = re.compile(r"[0-9a-f]{4,16}")
RE_BRU = re.compile(r"KRN-(\d+)")


def resolver(ref, base_dados=None):
    """Ref -> id da execução: prefixo hex (4 a 16) ou `KRN-n` (a mais recente da issue); qualquer outra forma: None.
    Só a ref já validada pelas regex entra no SQL, sempre como parâmetro."""
    if not isinstance(ref, str):
        return None
    ref = ref.strip()
    con = conectar(base_dados)
    try:
        if RE_HEX.fullmatch(ref.lower()):
            r = con.execute("SELECT execucao_id FROM indexado WHERE execucao_id LIKE ? ORDER BY inicio DESC LIMIT 1",
                            (ref.lower() + "%",)).fetchone()
            if r is None:  # execução gravada só por `gravar`, sem indexado
                r = con.execute("SELECT execucao_id FROM passo WHERE execucao_id LIKE ? ORDER BY execucao_id LIMIT 1",
                                (ref.lower() + "%",)).fetchone()
            return r[0] if r else None
        m = RE_BRU.fullmatch(ref.upper())
        if m:
            r = con.execute("SELECT execucao_id FROM indexado WHERE issue=? ORDER BY inicio DESC LIMIT 1",
                            (f"KRN-{int(m.group(1))}",)).fetchone()
            return r[0] if r else None
        return None
    finally:
        con.close()


def _consulta(base_dados, sql, args=()):
    con = conectar(base_dados)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def linha_do_tempo(execucao_id, base_dados=None):
    return _consulta(base_dados, "SELECT * FROM passo WHERE execucao_id=? ORDER BY n", (execucao_id,))


def _resumo(p):
    if p["tipo"] == "ferramenta":
        return f"{p['ferramenta'] or '?'}: {_uma_linha(p['entrada'], 80)}"
    return f"{p['tipo']}: {_uma_linha(p['texto'] or p['saida'], 80)}"


def estado_no_passo(execucao_id, n, base_dados=None):
    """O passo `n`, o resumo dos anteriores (1..n-1) e as falas anteriores a ele. Passo inexistente: `passo` None."""
    todos = linha_do_tempo(execucao_id, base_dados)
    alvo = next((p for p in todos if p["n"] == n), None)
    antes = [p for p in todos if p["n"] < n]
    return {"passo": alvo, "anteriores": [{"n": p["n"], "tipo": p["tipo"], "resumo": _resumo(p)} for p in antes],
            "falas": [p["texto"] or "" for p in antes if p["tipo"] == "fala"]}


def _uma_linha(t, limite=120):
    t = " ".join(str(t if t is not None else "").split())
    return t[:limite - 1] + "…" if len(t) > limite else t


def _dur(p):
    return "—" if p["duracao_s"] is None else f"{p['duracao_s']:.1f}s"


def codex(eid, base_dados=None):
    """A execução é do Codex (pela conta gravada em `indexado`)?"""
    return any(eh_codex(r) for r in _consulta(base_dados, "SELECT conta FROM indexado WHERE execucao_id=?", (eid,)))


def _cabecalho(eid, base_dados):
    r = _consulta(base_dados, "SELECT * FROM indexado WHERE execucao_id=?", (eid,))
    r = r[0] if r else {}
    ok = r.get("ok")
    res = "—" if ok is None else ("ok" if ok else "falhou")
    return f"#{eid} {r.get('issue') or r.get('nome') or '—'} {r.get('conta') or '—'} {r.get('modelo') or '—'} {res}"


def ver(ref, passo=None, base_dados=None) -> str:
    eid = resolver(ref, base_dados)
    if eid is None:
        return f"Execução não encontrada para a referência {_uma_linha(ref, 40)!r}."
    cab = _cabecalho(eid, base_dados)
    if passo is None:
        linhas = []
        for p in linha_do_tempo(eid, base_dados):
            nome = p["ferramenta"] or p["tipo"]
            ent = _uma_linha(p["entrada"] if p["tipo"] == "ferramenta" else p["texto"])
            linhas.append(f"{p['n']} {nome} {ent} → {_uma_linha(p['saida'])} {_dur(p)}")
        if codex(eid, base_dados):
            linhas.insert(0, CODEX_SEM_SAIDA if linhas else CODEX_VAZIO)
        return "\n".join([cab] + linhas)
    est = estado_no_passo(eid, passo, base_dados)
    p = est["passo"]
    if p is None:
        return f"{cab}\nO passo {passo} não existe nesta execução."
    out = [cab, f"Passo {p['n']} ({p['tipo']}) {p['ferramenta'] or ''} duração {_dur(p)}".replace("  ", " "),
           "Entrada:", p["entrada"] or p["texto"] or "—", "Saída:", p["saida"] if p["saida"] is not None else "—"]
    if est["anteriores"]:
        out.append("Passos anteriores:")
        out += [f"{a['n']} {a['resumo']}" for a in est["anteriores"]]
    if est["falas"]:
        out.append("Falas anteriores:")
        out += est["falas"]
    return "\n".join(out)


def _desatualizado(ja, reg, tamanho):
    """Linha de `indexado` a refazer: não existe, o stream mudou de tamanho, ou veio da Task 1 sem o cabeçalho."""
    if not ja or ja["tamanho"] != tamanho or ja["inicio"] is None:
        return True
    return ja["issue"] is None and issue_de(reg.get("nome")) is not None


def apagar_velhos(con, agora=None):
    """Retenção: passos e cabeçalho de execução com início há mais de RETENCAO_DIAS dias saem do banco. Execução sem
    início legível (gravada só por `gravar`) fica. Devolve quantas execuções saíram."""
    corte = (agora or conta.agora()) - dt.timedelta(days=RETENCAO_DIAS)
    velhas = [r[0] for r in con.execute("SELECT execucao_id, inicio FROM indexado").fetchall()
              if (t := _ts(r[1])) is not None and t < corte]
    with con:
        for eid in velhas:
            con.execute("DELETE FROM passo WHERE execucao_id=?", (eid,))
            con.execute("DELETE FROM indexado WHERE execucao_id=?", (eid,))
    return len(velhas)


def indexar_novos(desde_dias=3, limite=200, base_dados=None) -> int:
    """Percorre o uso.jsonl dos últimos dias, do mais recente ao mais antigo, e indexa o stream de cada execução nova ou
    mudada. Devolve quantas gravaram passos; stream ilegível ou vazio fica marcado em `indexado` (0 passos, com o tamanho)
    para não ser retentado a cada rodada e não gasta o limite."""
    desde = conta.agora() - dt.timedelta(days=desde_dias)
    feitos = 0
    con = conectar(base_dados)
    try:
        con.row_factory = sqlite3.Row
        apagar_velhos(con)
        for reg in sorted(conta.ler_uso(desde, base_dados), key=lambda r: r["inicio"], reverse=True):
            if feitos >= limite:
                break
            arq = _arq_stream(reg)
            try:
                tamanho = arq.stat().st_size if arq else None
            except OSError:
                tamanho = None
            if tamanho is None:
                continue
            eid = execucao_id(reg)
            ja = con.execute("SELECT * FROM indexado WHERE execucao_id=?", (eid,)).fetchone()
            if not _desatualizado(ja, reg, tamanho):
                continue
            if indexar_stream(reg, base_dados) > 0:
                feitos += 1
                continue
            with con:  # 0 passos: marca para não retentar (stream malformado, vazio ou sem eventos)
                con.execute("DELETE FROM passo WHERE execucao_id=?", (eid,))
                _marcar(con, reg, eid, arq, tamanho)
    finally:
        con.close()
    return feitos


# ---- replay: rodar de novo a partir de um passo, com a correção de quem pediu (dono ou operador) ----

CORRECAO_MAX = 2000  # caracteres da correção
REPLAY_TRECHO = 2000  # entrada e saída do passo N no contexto
REPLAY_ANTERIORES = 40  # resumos dos últimos passos anteriores
REPLAY_FALAS, REPLAY_FALA_MAX = 3, 600
INI_RASTREIO, FIM_RASTREIO = "<<<INICIO_RASTREIO_DA_EXECUCAO>>>", "<<<FIM_RASTREIO_DA_EXECUCAO>>>"
INI_CORRECAO, FIM_CORRECAO = "<<<INICIO_CORRECAO_DO_USUARIO>>>", "<<<FIM_CORRECAO_DO_USUARIO>>>"
RE_DELIM = re.compile(r"<{3,}|>{3,}")


def _sem_invisiveis(t):
    """NFKC (＜ e ﹤ viram <) e fora os caracteres de formato (Cf: ZWSP, ZWJ, BOM, marcas de direção); controle (Cc)
    vira espaço, menos a quebra de linha. Assim `<\u200b<<` ou `＜＜＜` não escapam da neutralização dos delimitadores."""
    t = unicodedata.normalize("NFKC", t)
    return "".join("" if unicodedata.category(c) == "Cf" else
                   (" " if unicodedata.category(c) == "Cc" and c != "\n" else c) for c in t)


def _dado(texto, limite):
    """Texto que entra entre delimitadores: normalizado (NFKC, sem invisíveis), limpo de segredo, sem `<<<`/`>>>` (não
    fecha o bloco antes da hora) e cortado no limite. Nada é interpretado: markup fica como texto."""
    t = limpar(_sem_invisiveis(str(texto if texto is not None else ""))) or ""
    t = RE_DELIM.sub(lambda m: ("‹" if m.group(0)[0] == "<" else "›") * 3, t)
    return t[:limite - 1] + "…" if len(t) > limite else t


def _via(quem, via=None):
    return via or ("CLI" if not quem or str(quem).startswith("cli:") else "painel")


def contexto_replay(execucao_id, passo, correcao, base_dados=None, quem=None, via=None) -> str:
    """Estado da execução até o passo N (resumo dos anteriores, falas recentes, ferramenta/entrada/saída do passo N) e
    a correção de quem pediu (login, via painel ou CLI), cada um entre delimitadores. Vai para o item da fila;
    `agenda.montar_prompt` põe no prompt."""
    est = estado_no_passo(execucao_id, int(passo), base_dados)
    p = est["passo"] or {}
    linhas = [f"Execução #{execucao_id}, passo {int(passo)}.", INI_RASTREIO]
    ant = est["anteriores"][-REPLAY_ANTERIORES:]
    if len(est["anteriores"]) > len(ant):
        linhas.append(f"({len(est['anteriores']) - len(ant)} passos anteriores omitidos)")
    if ant:
        linhas.append("Passos anteriores (resumo):")
        linhas += [f"{a['n']} {_dado(a['resumo'], 120)}" for a in ant]
    if est["falas"]:
        linhas.append("Últimas falas do agente antes do passo:")
        linhas += [f"- {_dado(f, REPLAY_FALA_MAX)}" for f in est["falas"][-REPLAY_FALAS:]]
    linhas += [f"Passo {int(passo)} ({_dado(p.get('tipo'), 40)}) {_dado(p.get('ferramenta'), 80)}".rstrip(),
               "Entrada:", _dado(p.get("entrada") or p.get("texto") or "—", REPLAY_TRECHO),
               "Saída" + (" (erro)" if p.get("erro") else "") + ":",
               _dado(p.get("saida") if p.get("saida") is not None else "—", REPLAY_TRECHO),
               FIM_RASTREIO, f"Correção pedida por {_dado(quem or 'cli', 80)}, via {_dado(_via(quem, via), 10)}:",
               INI_CORRECAO, _dado(correcao, CORRECAO_MAX), FIM_CORRECAO]
    return "\n".join(linhas)


def _log(*a):
    print(conta.iso(conta.agora()), *a, flush=True)


def _freio(t, base_dados):
    """Teto de execuções ou disjuntor, só lidos (sem pausar, comentar nem chamar o orquestrador): a
    mensagem humana da recusa, ou None. Leitura que falha não barra (a fila confere de novo ao enfileirar)."""
    from hub import agenda, retomada
    try:
        freio = retomada.teto(t.nome, t.linear.get("ultima_fala"), base_dados)
        if freio:
            return (f"Replay recusado: teto de execuções ({freio}). Replay não libera o teto: peça ao orquestrador "
                    "para decidir (ele já foi avisado pela fila) e tente de novo depois.")
        freio = agenda.em_loop(t.nome, base_dados, t.linear.get("ultima_fala"), t.linear.get("estado_desde"))
        if freio:
            return (f"Replay recusado: disjuntor ({freio}). Replays seguidos contam como voltas sem fala nova; "
                    "aguarde a pausa do disjuntor acabar ou peça ao orquestrador para decidir.")
    except Exception:  # noqa: BLE001 — explicar a recusa nunca derruba o pedido
        pass
    return None


def _ocupada(nome, base_dados):
    """Mensagem se a issue está rodando agora ou já tem item na fila (o replay nunca sobrescreve: só com ela ociosa)."""
    from hub import agenda
    if any(r.get("nome") == nome for r in agenda.rodando_todas(base_dados).values()):
        return "A issue está em execução agora; espere terminar e peça o replay de novo."
    if any(i.get("nome") == nome for i in agenda.fila(base_dados)):
        return "A issue já está na fila; espere ela rodar e peça o replay de novo."
    return None


def rodar_de_novo(ref, passo, correcao, base_dados=None, buscar_issue=None, enfileirar_fn=None, quem=None,
                  via=None) -> dict:
    """Põe a issue da execução `ref` de novo na fila, com o estado até o passo N e a correção de `quem` no prompt.
    Só com a issue ociosa (nem rodando nem na fila). Nunca levanta: recusa (issue ausente ou ocupada, passo inexistente,
    teto ou disjuntor) volta como {"ok": False, "motivo"}."""
    from hub import linear
    r = _rodar_de_novo(ref, passo, correcao, base_dados, buscar_issue or linear.item_por_ident, enfileirar_fn,
                       quem, via)
    _log(f"replay: {_uma_linha(quem or 'cli', 80)} pediu {_uma_linha(ref, 40)} passo {_uma_linha(passo, 12)}: "
         f"{'ok' if r['ok'] else 'recusado'}; {r['motivo']}")
    return r


def issue_da_execucao(eid, base_dados=None):
    """`KRN-n` da execução (pelo cabeçalho em `indexado`), ou None."""
    cab = _consulta(base_dados, "SELECT nome, issue FROM indexado WHERE execucao_id=?", (eid,))
    return (cab[0]["issue"] or issue_de(cab[0]["nome"])) if cab else None


def _rodar_de_novo(ref, passo, correcao, base_dados, buscar_issue, enfileirar_fn, quem=None, via=None):
    from hub import linear
    if isinstance(passo, bool) or not isinstance(passo, int) or passo < 1:
        return {"ok": False, "motivo": "Passo inválido: use o número do passo (1, 2, ...)."}
    correcao = (correcao or "").strip() if isinstance(correcao, str) else ""
    if not correcao:
        return {"ok": False, "motivo": "Escreva a correção (o que fazer diferente a partir deste passo)."}
    if len(correcao) > CORRECAO_MAX:
        return {"ok": False, "motivo": f"A correção passa de {CORRECAO_MAX} caracteres ({len(correcao)}); encurte."}
    eid = resolver(ref, base_dados)
    if eid is None:
        return {"ok": False, "motivo": f"Execução não encontrada para a referência {_uma_linha(ref, 40)!r}."}
    if estado_no_passo(eid, passo, base_dados)["passo"] is None:
        return {"ok": False, "motivo": f"O passo {passo} não existe na execução #{eid}."}
    ident = issue_da_execucao(eid, base_dados)
    if not ident:
        return {"ok": False, "motivo": f"A execução #{eid} não é de uma issue do Linear: o replay só reenfileira issue."}
    nome = f"linear-{ident.lower()}"
    try:
        ocupada = _ocupada(nome, base_dados)
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "motivo": f"Não consegui conferir a fila agora ({type(ex).__name__})."}
    if ocupada:
        return {"ok": False, "motivo": ocupada}
    try:
        item = buscar_issue(ident)
    except Exception as ex:  # noqa: BLE001 — Linear fora do ar vira mensagem
        return {"ok": False, "motivo": f"Não consegui ler a issue {ident} no Linear agora ({type(ex).__name__})."}
    if not item:
        return {"ok": False, "motivo": f"A issue {ident} não foi encontrada no Linear."}
    ctx = contexto_replay(eid, passo, correcao, base_dados, quem, via)
    item = {**item, "replay": {"execucao": eid, "passo": passo, "contexto": ctx}}
    t = linear.tarefa(item)
    freio = _freio(t, base_dados)  # antes da fila: recusa limpa, sem a pausa e o comentário que o enfileirar dispara
    if freio:
        return {"ok": False, "motivo": freio}
    motivo = f"replay do passo {passo}"
    from hub import agenda
    try:
        agenda._pasta(base_dados).mkdir(parents=True, exist_ok=True)  # a trava mora lá
        # sob a trava do despacho: nenhuma conta tira a issue da fila e a marca como rodando entre a conferência e a fila
        with agenda._trava_despacho(base_dados) as livre:
            if not livre:
                return {"ok": False, "motivo": "Despacho ocupado; tente em instantes."}
            ocupada = _ocupada(nome, base_dados)
            if ocupada:
                return {"ok": False, "motivo": ocupada}
            if enfileirar_fn is None:
                ok = agenda.enfileirar(t, motivo, base_dados)  # sem substituir: o que já está na fila fica
            else:
                ok = enfileirar_fn(t, motivo, base_dados, substituir=False)
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "motivo": f"A fila não aceitou o replay agora ({type(ex).__name__})."}
    if not ok:
        with contextlib.suppress(Exception):
            ocupada = _ocupada(nome, base_dados)
        return {"ok": False, "motivo": ocupada or _freio(t, base_dados)
                or "A fila recusou o replay (teto de execuções ou disjuntor). Veja o log do hub."}
    return {"ok": True, "motivo": f"{ident} na fila: roda de novo a partir do passo {passo} da execução #{eid}."}


def main(argv=None, base_dados=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="hub.rastreio", description="Rastreio da execução do agente")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("ver", help="passos de uma execução (id hex ou KRN-n)")
    v.add_argument("ref")
    v.add_argument("--passo", type=int)
    i = sub.add_parser("indexar", help="indexa os streams novos do uso.jsonl")
    i.add_argument("--dias", type=int, default=3)
    r = sub.add_parser("reenfileirar", help="roda a issue de novo a partir de um passo, com a sua correção")
    r.add_argument("ref")
    r.add_argument("--passo", type=int, required=True)
    r.add_argument("--correcao", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "ver":
        txt = ver(a.ref, a.passo, base_dados)
        print(txt)
        return 1 if txt.startswith("Execução não encontrada") else 0
    if a.cmd == "reenfileirar":
        import getpass
        from hub import usuarios
        quem = f"cli:{getpass.getuser()}"
        res = rodar_de_novo(a.ref, a.passo, a.correcao, base_dados, quem=quem, via="CLI")
        usuarios.auditar(quem, "replay", f"{_uma_linha(a.ref, 40)} passo {a.passo}",
                         "ok" if res["ok"] else f"erro: {res['motivo']}")
        print(res["motivo"])
        return 0 if res["ok"] else 1
    n = indexar_novos(a.dias, base_dados=base_dados)
    print(f"{n} execuções indexadas.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
