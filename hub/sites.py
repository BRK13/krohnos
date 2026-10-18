"""Cadastro dos sites: uma fonte de verdade por site, em sites/<id>.json.

    python -m hub.sites listar            # id, ativo, domínio
    python -m hub.sites validar           # falha alto, com o campo e o arquivo, se algo estiver errado
    python -m hub.sites resumo [--todos]  # texto para o prompt das tarefas do Claude (ids, caminhos, regras, janelas)

Todo script do hub lê os sites daqui; nenhum id fica colado em prompt ou script.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from hub import RAIZ, codigo, config, data

PASTA = config.projetos()
CONSELHOS = ("CFP", "COFFITO", "OAB")
# Onde as buscas do AI Overview são feitas (códigos de local da DataForSEO). O Brasil fica sem sufixo de pasta.
# Cidade serve para negócio local (a fsite05 busca como quem está em Brasília).
LOCAIS = {2076: "brasil", 1001541: "brasilia", 2620: "portugal", 2840: "eua", 2372: "irlanda", 2826: "reino-unido", 2124: "canada",
          2276: "alemanha", 2380: "italia", 2724: "espanha", 2392: "japao", 2036: "australia"}

RE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RE_HOST = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
RE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RE_GSC = re.compile(r"^(sc-domain:[a-z0-9.-]+|https://[a-z0-9.-]+/)$")
RE_GA4 = re.compile(r"^\d{6,12}$")
RE_MEDICAO = re.compile(r"^G-[A-Z0-9]{6,14}$")
RE_CLARITY = re.compile(r"^[a-z0-9]{6,14}$")
RE_GMB_CID = re.compile(r"^(\d{5,25}|sem_perfil)$")  # sem_perfil: conferido que o negócio não tem Perfil da Empresa no Google
RE_CHAVE_INDEXNOW = re.compile(r"^[a-zA-Z0-9-]{8,128}$")


class SiteInvalido(ValueError):
    """Cadastro com erro. A mensagem lista cada campo errado, com o arquivo. `campos` ({campo: mensagem}) só vem de gravar()."""
    campos = {}


def _tipo(v, tipo):
    if tipo == "str?":
        return v is None or isinstance(v, str)
    if tipo == "str":
        return isinstance(v, str) and v.strip() != ""
    if tipo == "bool":
        return isinstance(v, bool)
    if tipo == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if tipo == "lista":
        return isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)
    if tipo == "dict":
        return isinstance(v, dict)
    if tipo == "dict?":
        return v is None or isinstance(v, dict)
    raise AssertionError(tipo)


CAMPOS = {
    "id": "str", "ids_antigos": "lista", "ativo": "bool", "nome": "str", "dominio": "str",
    "hosts_alternativos": "lista", "hosts_citacao": "lista", "repo": "dict?", "ids": "dict", "marca": "dict",
    "conselho": "str?", "contexto": "str", "frase_canonica": "str?", "meta": "str?", "concorrentes": "lista",
    "monitor": "dict?", "padrao": "dict", "ranking": "dict?",
}
CAMPOS_OPCIONAIS = {"medicao": "dict?", "fichas_concorrentes": "lista"}  # fichas_concorrentes (hub/concorrentes.py): "Nome | dominio | x:perfil | linkedin:company/slug"; medicao: eventos e tags que o site manda ao GA4/Clarity; sem ele o coletor não puxa evento
CAMPOS_MEDICAO = {"eventos": "lista", "tags": "lista", "funis": None}
CAMPOS_IDS = ("gsc", "ga4", "ga4_medicao", "clarity", "gmb_cid", "dokploy_app")
CAMPOS_MONITOR = {"buscas": "lista", "painel": "lista", "locais": None, "max_buscas": "int", "max_prompts_painel": "int",
                  "repeticoes_sem_nome": "int", "termos_gsc": "lista"}
CAMPOS_RANKING = {"palavras": "lista", "local": "int", "idioma": "str", "dispositivo": "str", "profundidade": "int"}
CAMPOS_PADRAO = {"fora_do_google": "bool", "noindex_permitido": "lista", "treino_ia": "str?", "tipo_negocio": "str?",
                 "paginas_amostra": "lista", "indexnow": "dict?"}


def validar(s, origem="site"):
    """Lista de erros (vazia = ok). Não corrige nada: cadastro errado tem que ser consertado no arquivo."""
    if not isinstance(s, dict):
        return [f"{origem}: o arquivo precisa ser um objeto JSON"]
    erros = []
    err = erros.append
    for campo, tipo in CAMPOS.items():
        if campo not in s:
            err(f"{origem}: falta o campo '{campo}'")
        elif not _tipo(s[campo], tipo):
            err(f"{origem}: '{campo}' deveria ser {tipo}, veio {json.dumps(s[campo], ensure_ascii=False)[:80]}")
    for campo, tipo in CAMPOS_OPCIONAIS.items():
        if campo in s and not _tipo(s[campo], tipo):
            err(f"{origem}: '{campo}' deveria ser {tipo}")
    for extra in sorted(set(s) - set(CAMPOS) - set(CAMPOS_OPCIONAIS)):
        err(f"{origem}: campo desconhecido '{extra}' (erro de digitação?)")
    if erros:
        return erros
    if not RE_ID.match(s["id"]):
        err(f"{origem}: 'id' só aceita minúsculas, números e hífen: {s['id']!r}")
    for antigo in s["ids_antigos"]:
        if not RE_ID.match(antigo):
            err(f"{origem}: 'ids_antigos' inválido: {antigo!r}")
    for campo in ("dominio",):
        if not RE_HOST.match(s[campo]):
            err(f"{origem}: '{campo}' tem que ser só o host, sem esquema, caminho ou maiúscula: {s[campo]!r}")
    for campo in ("hosts_alternativos", "hosts_citacao"):
        for h in s[campo]:
            if not RE_HOST.match(h):
                err(f"{origem}: '{campo}' tem host inválido (sem esquema ou caminho): {h!r}")
            elif h == s["dominio"]:
                err(f"{origem}: '{campo}' repete o domínio canônico {h!r}")
    if s["conselho"] is not None and s["conselho"] not in CONSELHOS:
        err(f"{origem}: 'conselho' tem que ser um de {', '.join(CONSELHOS)} ou null: {s['conselho']!r}")
    if s["repo"] is not None:
        for campo in ("github", "base", "pacotes"):
            if not _tipo(s["repo"].get(campo), "str"):
                err(f"{origem}: 'repo.{campo}' é obrigatório")
        if s["repo"].get("pacotes") not in (None, "npm", "yarn", "pnpm", "bun"):
            err(f"{origem}: 'repo.pacotes' desconhecido: {s['repo'].get('pacotes')!r}")
    ids = s["ids"]
    for extra in sorted(set(ids) - set(CAMPOS_IDS)):
        err(f"{origem}: 'ids.{extra}' desconhecido")
    for campo, regra in (("gsc", RE_GSC), ("ga4", RE_GA4), ("ga4_medicao", RE_MEDICAO), ("clarity", RE_CLARITY), ("gmb_cid", RE_GMB_CID)):
        v = ids.get(campo)
        if v is not None and (not isinstance(v, str) or not regra.match(v)):
            err(f"{origem}: 'ids.{campo}' com formato inválido: {v!r}")
    marca = s["marca"]
    if not _tipo(marca.get("nome"), "str"):
        err(f"{origem}: 'marca.nome' é obrigatório")
    if not _tipo(marca.get("aliases", None), "lista"):
        err(f"{origem}: 'marca.aliases' tem que ser lista de texto")
    if not _tipo(marca.get("nome_generico"), "bool"):
        err(f"{origem}: 'marca.nome_generico' tem que ser true ou false")
    elif marca["nome_generico"] and not marca.get("aliases"):
        err(f"{origem}: marca com nome genérico precisa de aliases (o nome sozinho não conta como menção)")
    if marca.get("identificador") is not None:
        try:
            re.compile(marca["identificador"])
        except (re.error, TypeError) as e:
            err(f"{origem}: 'marca.identificador' não é regex válida: {e}")
    if s["monitor"] is not None:
        m = s["monitor"]
        for campo, tipo in CAMPOS_MONITOR.items():
            if campo not in m:
                err(f"{origem}: falta 'monitor.{campo}'")
            elif tipo and not _tipo(m[campo], tipo):
                err(f"{origem}: 'monitor.{campo}' deveria ser {tipo}")
        locais = m.get("locais")
        if not isinstance(locais, list) or not locais or any(x not in LOCAIS for x in locais):
            err(f"{origem}: 'monitor.locais' tem que listar códigos conhecidos: {sorted(LOCAIS)}")
        if isinstance(m.get("buscas"), list) and not m["buscas"]:
            err(f"{origem}: 'monitor.buscas' vazio: sem busca não há o que monitorar")
        if isinstance(m.get("termos_gsc"), list) and not m["termos_gsc"]:
            err(f"{origem}: 'monitor.termos_gsc' vazio: não há termo padrão, cada site declara os seus")
        for extra in sorted(set(m) - set(CAMPOS_MONITOR)):
            err(f"{origem}: 'monitor.{extra}' desconhecido")
    med = s.get("medicao")
    if med:
        for campo, tipo in CAMPOS_MEDICAO.items():
            if campo not in med:
                err(f"{origem}: falta 'medicao.{campo}'")
            elif tipo and not _tipo(med[campo], tipo):
                err(f"{origem}: 'medicao.{campo}' deveria ser {tipo}")
        for extra in sorted(set(med) - set(CAMPOS_MEDICAO)):
            err(f"{origem}: 'medicao.{extra}' desconhecido")
        eventos = med.get("eventos")
        if isinstance(eventos, list) and (not eventos or len(set(eventos)) != len(eventos)):
            err(f"{origem}: 'medicao.eventos' não pode ser vazio nem ter evento repetido")
        for f in med.get("funis") or []:
            etapas = f.get("etapas") if isinstance(f, dict) else None
            if not (isinstance(f, dict) and _tipo(f.get("nome"), "str") and _tipo(etapas, "lista") and len(etapas) >= 2
                    and isinstance(eventos, list) and set(etapas) <= set(eventos)):
                err(f"{origem}: 'medicao.funis' precisa de {{nome, etapas}} com 2+ etapas, todas em 'medicao.eventos': {f!r}")
    r = s["ranking"]
    if r is not None:
        for campo, tipo in CAMPOS_RANKING.items():
            if campo not in r:
                err(f"{origem}: falta 'ranking.{campo}'")
            elif not _tipo(r[campo], tipo):
                err(f"{origem}: 'ranking.{campo}' deveria ser {tipo}")
        for extra in sorted(set(r) - set(CAMPOS_RANKING)):
            err(f"{origem}: 'ranking.{extra}' desconhecido")
        pal = r.get("palavras")
        if isinstance(pal, list) and (not pal or len(set(pal)) != len(pal)):
            err(f"{origem}: 'ranking.palavras' não pode ser vazio nem ter palavra repetida")
        if "dispositivo" in r and r["dispositivo"] not in ("mobile", "desktop"):
            err(f"{origem}: 'ranking.dispositivo' tem que ser mobile ou desktop")
        if _tipo(r.get("profundidade"), "int") and not 10 <= r["profundidade"] <= 100:
            err(f"{origem}: 'ranking.profundidade' tem que ficar entre 10 e 100")
    p = s["padrao"]
    for campo, tipo in CAMPOS_PADRAO.items():
        if campo not in p:
            err(f"{origem}: falta 'padrao.{campo}'")
        elif not _tipo(p[campo], tipo):
            err(f"{origem}: 'padrao.{campo}' deveria ser {tipo}")
    if "cookie_nao_medir" in p and not re.match(r"^[A-Za-z0-9_-]{1,64}$", str(p["cookie_nao_medir"])):
        err(f"{origem}: 'padrao.cookie_nao_medir' tem que ser o nome do cookie de opt-out (ex.: nao_medir)")
    if p.get("treino_ia") not in (None, "permitir", "bloquear"):
        err(f"{origem}: 'padrao.treino_ia' tem que ser permitir, bloquear ou null")
    for caminho in p.get("paginas_amostra") or []:
        if not caminho.startswith("/"):
            err(f"{origem}: 'padrao.paginas_amostra' usa caminho começando com '/': {caminho!r}")
    inx = p.get("indexnow")
    if isinstance(inx, dict):
        if not RE_CHAVE_INDEXNOW.match(str(inx.get("chave", ""))):
            err(f"{origem}: 'padrao.indexnow.chave' inválida")
        if inx.get("aviso") not in (None, "publicacao", "agendado", "manual"):
            err(f"{origem}: 'padrao.indexnow.aviso' tem que ser publicacao, agendado, manual ou null")
    if s["ativo"] and p.get("fora_do_google"):
        err(f"{origem}: site fora do Google não pode ser ativo (o verificador e o AI Overview não rodam nele)")
    return erros


def _ler(arq):
    """(site, erros) de um arquivo de site."""
    try:
        s = json.loads(arq.read_text(encoding="utf-8"))
    except ValueError as e:
        return None, [f"{arq.name}: JSON inválido: {e}"]
    e = validar(s, arq.name)
    if not e and s["id"] != arq.stem:
        e = [f"{arq.name}: o nome do arquivo tem que ser o id ({s['id']}.json)"]
    return s, e


def _campo_do_erro(msg):
    """'arquivo: 'ids.gsc' com formato...' -> 'ids.gsc'; sem campo identificável, ''."""
    m = re.search(r"'([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)'", msg)
    return m.group(1) if m else ""


def _recusa(campos):
    ex = SiteInvalido("; ".join(f"{c or 'cadastro'}: {m}" for c, m in campos.items()))
    ex.campos = campos
    return ex


def gravar(site_dict, pasta=None, criar=False):
    """Grava sites/<id>.json (atômico: tmp + os.replace) depois de `validar`. criar=True recusa id que já existe.
    Recusa host ou id já usado por outro site da pasta. Erro: SiteInvalido com `.campos` {campo: texto em português}."""
    pasta = Path(pasta or PASTA)
    sid = site_dict.get("id") if isinstance(site_dict, dict) else None
    if not isinstance(sid, str) or not RE_ID.match(sid):  # antes de tudo: o id vira nome de arquivo (nada de ../)
        raise _recusa({"id": "use só minúsculas, números e hífen (ex.: loja-exemplo)"})
    campos = {}
    for msg in validar(site_dict, f"{sid}.json"):
        texto = msg.split(": ", 1)[-1]
        vazio = re.search(r"deveria ser str, veio (\"\"|\"\s+\"|null)$", texto)
        campos.setdefault(_campo_do_erro(msg), "este campo é obrigatório e está vazio" if vazio else texto)
    destino = pasta / f"{sid}.json"
    if criar and destino.exists():
        campos["id"] = f"já existe um projeto com o id {sid!r}"
    if not campos:
        usados = {}
        for arq in sorted(pasta.glob("*.json")):
            if arq.stem == sid:
                continue
            try:
                outro, erros = _ler(arq)
            except OSError:
                continue
            if erros:
                continue
            for k in _chaves(outro):
                usados.setdefault(k, outro["id"])
        for k in {sid, *site_dict["ids_antigos"]} & usados.keys():
            campos["id"] = f"o id {k!r} já é de {usados[k]}"
        for h in hosts(site_dict):
            if "host:" + h in usados:
                campos["dominio"] = f"o host {h!r} já pertence a {usados['host:' + h]}"
    if campos:
        raise _recusa(campos)
    pasta.mkdir(parents=True, exist_ok=True)
    fd, nome_tmp = tempfile.mkstemp(dir=pasta, prefix=f".{sid}.", suffix=".tmp")  # nome único: duas gravações não se pisam
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(site_dict, ensure_ascii=False, indent=2) + "\n")
        os.chmod(nome_tmp, 0o644)
        if criar:
            try:
                os.link(nome_tmp, destino)  # falha se o id já existe (criação exclusiva, vale contra corrida)
            except FileExistsError:
                raise _recusa({"id": f"já existe um projeto com o id {sid!r}"}) from None
        else:
            os.replace(nome_tmp, destino)
    finally:
        try:
            os.unlink(nome_tmp)
        except FileNotFoundError:
            pass
    return destino


def _chaves(s):
    return {s["id"], *s["ids_antigos"], *("host:" + h for h in hosts(s))}


def _do_mcp(cadastro):
    """Sites criados pelo create_project do MCP. O cadastro do git vence; arquivo ruim é pulado com aviso, nunca derruba a carga."""
    usados = set().union(*map(_chaves, cadastro))
    saida = []
    for arq in sorted((data() / "mcp" / "projetos" / "sites").glob("*.json")):
        try:
            s, e = _ler(arq)
        except OSError as erro:
            s, e = None, [f"{arq.name}: {erro}"]
        if not e and _chaves(s) & usados:
            e = [f"{arq.name}: id ou host já está no cadastro"]
        if e:
            print("sites: site do MCP pulado: " + "; ".join(e), file=sys.stderr)
            continue
        usados |= _chaves(s)
        saida.append(s)
    return saida


def carregar(pasta=None):
    """Todos os sites, validados. Qualquer erro em qualquer arquivo do cadastro derruba a carga inteira.
    Sem pasta, inclui os sites criados pelo MCP ($HUB_DATA/mcp/projetos/sites); origem(s) diz de onde veio."""
    sites = _carregar(Path(pasta or PASTA))
    return sites if pasta else sites + _do_mcp(sites)


def origem(s):
    return "cadastro" if (PASTA / f"{s['id']}.json").is_file() else "mcp"


def _carregar(pasta):
    arquivos = sorted(pasta.glob("*.json"))
    if not arquivos:
        raise SiteInvalido(f"nenhum site em {pasta}")
    sites, erros = [], []
    for arq in arquivos:
        s, e = _ler(arq)
        erros += e
        if not e:
            sites.append(s)
    vistos = {}
    for s in sites:
        for chave in [s["id"], *s["ids_antigos"]]:
            if chave in vistos and vistos[chave] != s["id"]:
                erros.append(f"{s['id']}.json: id {chave!r} já usado por {vistos[chave]}.json")
            vistos.setdefault(chave, s["id"])
        for h in [s["dominio"], *s["hosts_alternativos"], *s["hosts_citacao"]]:
            dono = vistos.get("host:" + h)
            if dono and dono != s["id"]:
                erros.append(f"{s['id']}.json: host {h!r} já pertence a {dono}.json")
            vistos.setdefault("host:" + h, s["id"])
    if erros:
        raise SiteInvalido("cadastro de sites com erro:\n- " + "\n- ".join(erros))
    return sites


def ativos(pasta=None):
    return [s for s in carregar(pasta) if s["ativo"]]


def monitorados(pasta=None, site_id=None):
    """Sites ativos com AI Overview/painel configurado (opcionalmente só um)."""
    saida = [s for s in ativos(pasta) if s["monitor"]]
    if site_id:
        saida = [s for s in saida if s["id"] == site_id]
        if not saida:
            raise SiteInvalido(f"site {site_id!r} não existe, não está ativo ou não tem monitor")
    return saida


def site(site_id, pasta=None):
    for s in carregar(pasta):
        if s["id"] == site_id:
            return s
    raise SiteInvalido(f"site {site_id!r} não existe em sites/")


def mapa_ids_antigos(pasta=None):
    """{id antigo: id novo}, para migrar pastas de dados e credenciais."""
    return {antigo: s["id"] for s in carregar(pasta) for antigo in s["ids_antigos"] if antigo != s["id"]}


def hosts(s):
    """Hosts que são do próprio site: citação de qualquer um deles é citação do site."""
    return [s["dominio"], *s["hosts_alternativos"], *s["hosts_citacao"]]


def conselhos():
    return json.loads((RAIZ / "conselhos.json").read_text(encoding="utf-8"))


def config_hub():
    return config.ler_hub_json()


def contexto_completo(s):
    """Contexto do site. Sem as regras do conselho (o dono, 10/10/2026: travavam os agentes; conselhos.json fica só
    como referência, fora dos prompts)."""
    return s["contexto"]


def marca_curta(s):
    return s["marca"]["aliases"][0] if s["marca"]["aliases"] and not s["marca"]["nome_generico"] else s["marca"]["nome"]


REGRAS_PUBLICACAO = """## Regras de publicação (toda tarefa que publica ou edita página ou artigo)
- Antes de editar página no ar, guarde a versão atual (o arquivo no git antes do commit, ou o HTML/texto num arquivo na sua pasta de saída).
- Antes de publicar, responda: "o leitor escolheria esta página em vez das 3 primeiras do Google?". Compare com a SERP real (MCP do hub, `get_serp_results`) e diga em que ela ganha. Se não ganha, não publique e diga por quê.
- Todo artigo tem caixa de autor (nome real, registro profissional se houver, redes) e schema de autor (Person) e Organization; 2 a 3 links internos para páginas que existem.
- Nunca invente dado, número, fonte, autor ou avaliação.
- Nunca ranqueie a própria marca em 1º numa lista ou comparação: critério declarado, e diga em que o concorrente ganha.
- Nunca crie página de cidade ou bairro onde não há atendimento real.
- Site novo (marcado "Cadência" na lista de sites): no máximo 4 páginas novas por semana, somando todas as tarefas; a conta vem das janelas `pagina_nova`. Chegou a 4: não publique página nova, melhore uma existente.
"""
CADENCIA_MAX, CADENCIA_CLIQUES = 4, 30  # site com menos de 30 cliques orgânicos em 28 dias ainda é "site novo"


def cadencia(site_id, base_dados=None, hoje=None):
    """(cliques orgânicos nos 28 dias até o último dia do armazém, páginas novas nos últimos 7 dias pelas janelas)."""
    from hub import armazem, janelas
    hoje = hoje or dt.date.today()
    fim = (armazem.ultimo(site_id, "gsc", "cliques", base_dados=base_dados) or (hoje.isoformat(),))[0]
    ini = (dt.date.fromisoformat(fim) - dt.timedelta(days=27)).isoformat()
    cliques = int(armazem.soma(site_id, "gsc", "cliques", ini, fim, base_dados=base_dados)[0])
    desde = (hoje - dt.timedelta(days=6)).isoformat()
    novas = sum(1 for m in janelas.ler(base_dados) if m["site"] == site_id and m["tipo"] == "pagina_nova" and m["data"] >= desde)
    return cliques, novas


JANELAS_MAX = 40  # tarefa com repo vê no máximo 40 janelas; o resto sai pelo comando


JANELAS_POR_SITE = 15  # tarefa sem repo: o cabeçalho de todos os sites vem completo, então as janelas cabem menos


def _janelas_compactas(lista, por_site=None):
    """Uma linha por (site, url, tipo), a mais recente primeiro, no máximo JANELAS_MAX (ou `por_site` por site, se dado);
    devolve (linhas, cortadas por site)."""
    vistas, linhas, cortadas, por = set(), [], {}, {}
    for m in lista:  # já vem da mais recente para a mais antiga
        chave = (m["site"], m["pagina"], m["tipo"])
        if chave in vistas:
            continue
        vistas.add(chave)
        cheio = por.get(m["site"], 0) >= por_site if por_site else len(linhas) >= JANELAS_MAX
        if cheio:
            cortadas[m["site"]] = cortadas.get(m["site"], 0) + 1
            continue
        por[m["site"]] = por.get(m["site"], 0) + 1
        linhas.append(f"- {m['site']} · {m['pagina'].removeprefix('https://')} · {m['tipo']} · julgar a partir de {m['julgar_em']}")
    return linhas, cortadas


def resumo(todos=False, pasta=None, base_dados=None, repos=None):
    """Texto colado no fim do prompt das tarefas do Claude: tudo que ela precisa saber dos sites, gerado do cadastro.
    `repos`: lista "dono/repo" da tarefa; só os sites desses repos vêm completos e com janelas, os outros em
    uma linha. `repos=None` mantém o texto completo. `repos=[]` (tarefa sem repo): cabeçalho completo de todo site ativo,
    janelas compactas (até JANELAS_POR_SITE por site) e a instrução de registrar."""
    from hub import janelas
    base = Path(base_dados or data())
    sites = carregar(pasta)
    linhas = [f"## Sites do hub (gerado de sites/*.json em {dt.date.today().isoformat()})", ""]
    alvo = None if repos is None else {r.lower() for r in repos}
    sem_repo = repos is not None and not repos
    do_repo = set()
    for s in sites:
        if not (todos or s["ativo"]):
            continue
        r = s["repo"] or {}
        if alvo is not None and not sem_repo and (r.get("github") or "").lower() not in alvo:
            linhas.append(f"- {s['nome']} ({s['id']}) · {s['dominio']} · {r.get('github') or 'sem repo'} · base {r.get('base') or '?'}")
            continue
        do_repo.add(s["id"])
        i = s["ids"]
        r = s["repo"] or {}
        linhas.append(f"### {s['nome']} (`{s['id']}`) · https://{s['dominio']}" + ("" if s["ativo"] else " · INATIVO, só monitoramento"))
        if r:
            linhas.append(f"- Repo GitHub {r['github']} · base `{r['base']}` · pacotes {r['pacotes']} · deploy {r.get('deploy') or '?'}")
            if r["github"].split("/")[0] != config.get("github.org"):
                linhas.append("- Repo de terceiro (do dono do site): PR sim, na base dele; merge só quando a issue tiver a label `merge`.")
        linhas.append(f"- Conselho: {s['conselho'] or 'nenhum'} · marca: {s['marca']['nome']}"
                      + (f" (aliases: {', '.join(s['marca']['aliases'])})" if s["marca"]["aliases"] else ""))
        n = len((s.get("ranking") or {}).get("palavras") or [])
        ids = [f"ranking: {n} palavras (semanal, hub)" if n else "ranking: sem palavras"]
        ids += [f"GSC `{i['gsc']}`" if i.get("gsc") else "GSC: sem acesso",
                f"GA4 `{i['ga4']}`" + (f" ({i['ga4_medicao']})" if i.get("ga4_medicao") else "") if i.get("ga4") else "GA4: sem acesso",
                f"Clarity `{i['clarity']}`" if i.get("clarity") else "Clarity: sem projeto"]
        linhas.append("- " + " · ".join(ids))
        if s["concorrentes"]:
            linhas.append(f"- Concorrentes: {', '.join(s['concorrentes'])} (mais os aprendidos em {base}/aio/{s['id']}/auto.json)")
        if s["padrao"]["fora_do_google"]:
            linhas.append("- Fora do Google de propósito (noindex intencional): não propor indexação.")
        cliques, novas = cadencia(s["id"], base_dados=base)
        if cliques < CADENCIA_CLIQUES:
            linhas.append(f"- Cadência: site novo ({cliques} cliques orgânicos em 28 dias): no máximo {CADENCIA_MAX} páginas novas por "
                          f"semana; nos últimos 7 dias já foram {novas}" + (" (limite atingido: nada de página nova)" if novas >= CADENCIA_MAX else ""))
        linhas.append(f"- Dados: {base}/aio/{s['id']}/ · {base}/gsc/{s['id']}/latest.md · {base}/verificador/{s['id']}/latest.md · "
                      f"{base}/snapshots/<data>/{s['id']}-*.json")
        linhas.append("")
    linhas += REGRAS_PUBLICACAO.splitlines() + [""]
    if alvo is None:
        linhas += janelas.texto_abertas(janelas.abertas(base_dados=base))
        return "\n".join(linhas).rstrip() + "\n"
    # tarefa com repos: só as janelas dos sites dela; sem repos, só o comando (as janelas completas não cabem em todo prompt)
    cmd = "`python3 -m hub.janelas abertas --site <id>`"
    if not do_repo:
        linhas += ["## Janelas de medição abertas", "", f"- não listadas aqui: antes de mexer numa página de site, confira as janelas dele com {cmd} "
                   f"(rodado em {codigo()}); página em janela aberta não muda de novo antes da data.", ""]
        return "\n".join(linhas).rstrip() + "\n"
    lista = [m for m in janelas.abertas(base_dados=base) if m["site"] in do_repo]
    corpo, cortadas = _janelas_compactas(lista, JANELAS_POR_SITE if sem_repo else None)
    linhas += ["## Janelas de medição abertas (não mexer de novo nestas páginas antes da data)", ""]
    linhas += corpo or ["- nenhuma mudança registrada em janela aberta"]
    for sid, n in sorted(cortadas.items()):
        linhas.append(f"- … +{n}: `cd {codigo()} && python3 -m hub.janelas abertas --site {sid}`")
    linhas += ["", "Para registrar uma mudança nova (todo PR de SEO): "
               "`python3 -m hub.janelas registrar --site <id> --url <página> --tipo <" + "|".join(janelas.PRAZOS) + "> --pr <url>` "
               f"rodado em {codigo()}.", ""]
    return "\n".join(linhas).rstrip() + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hub.sites", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listar")
    sub.add_parser("validar")
    r = sub.add_parser("resumo")
    r.add_argument("--todos", action="store_true", help="inclui os inativos")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # console do Windows não é UTF-8
    try:
        if args.cmd == "listar":
            for s in carregar():
                print(f"{s['id']:<18} {'ativo' if s['ativo'] else 'inativo':<8} {s['dominio']}")
        elif args.cmd == "validar":
            sites = carregar()
            print(f"ok: {len(sites)} sites, {sum(s['ativo'] for s in sites)} ativos")
        elif args.cmd == "resumo":
            sys.stdout.write(resumo(todos=args.todos))
    except SiteInvalido as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
