"""`kronos demo`: dados de demonstração inventados, para ver o painel cheio sem ligar nada.

    kronos demo [--dados DIR] [--semente N] [--fim AAAA-MM-DD]     popula
    kronos demo --limpar [--dados DIR]                              remove só o que está marcado demo

Grava no diretório de dados (hub.data(), ou --dados): os 2 projetos de exemplos/projetos, 30 dias de série inventada no
armazém do BI (random.Random(semente): mesma semente, mesmos números), 3 metas, 2 propostas e 1 recibo de exemplo.
A marca de tudo é o arquivo <dados>/demo.json (a lista exata do que foi criado, os projetos não aceitam campo extra) e o
campo `demo: true` nas metas e propostas. Rodar de novo não duplica; --limpar nunca toca em dado sem a marca.
Se o diretório já tem dado real (projeto, meta, proposta ou fato sem a marca), o comando RECUSA e explica: não há flag
para forçar. Nada aqui roda sozinho: só o comando explícito (as tarefas de exemplo não são copiadas, a agenda não as vê).
"""
import argparse
import datetime as dt
import json
import random
import re
import sys
from contextlib import closing
from pathlib import Path

from hub.ui import MARCA_DEMO as MARCADOR  # a tela lê a marca sem importar este módulo

DIAS = 30
SEMENTE = 852
RE_DIA = "%Y-%m-%d"
METRICAS = (("gsc", "impressoes"), ("gsc", "cliques"), ("gsc", "ctr"), ("gsc", "posicao"), ("ga4", "sessoes"), ("ga4", "conversoes"))

# (nível médio de impressões/dia, tendência por dia, CTR base, posição inicial, taxa de sessão por clique, conversão por sessão)
PERFIL = {"exemplo-loja": (1800, 14.0, 0.032, 14.0, 1.15, 0.021),
          "exemplo-clinica": (950, 7.5, 0.041, 9.5, 1.08, 0.034)}


def _dados(base):
    from hub import data
    return Path(base) if base else data()


def marcador(base=None):
    return _dados(base) / MARCADOR


def ativa(base=None):
    """Há dado de demonstração em `base` (ou no diretório de dados)?"""
    return marcador(base).is_file()


def _ler_marca(base):
    try:
        m = json.loads(marcador(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) and m.get("demo") is True else None


# ---------- o que a demo cria ----------

def _projetos():
    from hub import config
    return sorted((config.RAIZ / "exemplos" / "projetos").glob("*.json"))


def serie(semente, fim, dias=DIAS):
    """Fatos inventados dos 30 dias que terminam em `fim`. Só depende da semente e do índice do dia (não da data de hoje)."""
    rng = random.Random(semente)
    fatos = []
    for negocio in sorted(PERFIL):
        imp0, tend, ctr0, pos0, sess, conv = PERFIL[negocio]
        for i in range(dias):
            dia = (fim - dt.timedelta(days=dias - 1 - i)).strftime(RE_DIA)
            semana = 1.0 - (0.22 if (fim - dt.timedelta(days=dias - 1 - i)).weekday() >= 5 else 0.0)
            imp = max(1, round((imp0 + tend * i) * semana * rng.uniform(0.88, 1.12)))
            ctr = max(0.005, ctr0 * rng.uniform(0.9, 1.1) + 0.00012 * i)
            cliques = round(imp * ctr)
            sessoes = round(cliques * sess * rng.uniform(0.95, 1.05))
            conversoes = round(sessoes * conv * rng.uniform(0.7, 1.3))
            pos = round(max(1.0, pos0 - 0.07 * i + rng.uniform(-0.6, 0.6)), 1)
            for fonte, metrica, valor in (("gsc", "impressoes", imp), ("gsc", "cliques", cliques),
                                          ("gsc", "ctr", round(cliques / imp, 4)), ("gsc", "posicao", pos),
                                          ("ga4", "sessoes", sessoes), ("ga4", "conversoes", conversoes)):
                fatos.append({"dia": dia, "negocio": negocio, "fonte": fonte, "metrica": metrica, "valor": float(valor)})
    return fatos


def _metas(fim):
    d = lambda n: (fim - dt.timedelta(days=n)).strftime("%Y%m%d")
    t = lambda n: (fim - dt.timedelta(days=n)).strftime("%Y-%m-%dT09:00:00Z")
    base = {"demo": True, "issue": None, "url_issue": None, "origem": "demo", "acordada_em": None, "perguntas": []}
    pagespeed = lambda url: {"tipo": "pagespeed", "url": url, "metrica": "lcp_ms"}
    return [
        {**base, "id": f"m-{d(20)}-exemplo-loja-lcp", "objetivo": "LCP da home da Loja Exemplo abaixo de 2,5 s", "site": "exemplo-loja",
         "medir": pagespeed("https://loja.example.com/"), "alvo": 2500.0, "direcao": "menor", "unidade": "ms", "esforco": "medio",
         "estado": "ativa", "criada_em": t(20),
         "rodadas": [{"valor": v, "conta": True, "em": t(19 - 4 * n)} for n, v in enumerate((3800.0, 3350.0, 3100.0, 2850.0))]},
        {**base, "id": f"m-{d(25)}-exemplo-clinica-nota", "objetivo": "Nota de desempenho da Clínica Exemplo a partir de 90", "site": "exemplo-clinica",
         "medir": {"tipo": "pagespeed", "url": "https://clinica.example.com/", "metrica": "nota"}, "alvo": 90.0, "direcao": "maior",
         "unidade": "pontos", "esforco": "baixo", "estado": "batida", "criada_em": t(25),
         "rodadas": [{"valor": v, "conta": True, "em": t(24 - 5 * n)} for n, v in enumerate((71.0, 82.0, 91.0))]},
        {**base, "id": f"m-{d(3)}-hub-conversoes", "objetivo": "Dobrar as conversões mensais da Loja Exemplo", "site": "exemplo-loja",
         "medir": None, "alvo": None, "direcao": "maior", "unidade": "", "esforco": "alto", "estado": "definir",
         "criada_em": t(3), "rodadas": []},
    ]


def _propostas(fim):
    def p(n, estado, hora, destino, assunto, corpo):
        dia = (fim - dt.timedelta(days=n))
        pid = f"{dia:%Y%m%d}-{hora}-demo-email-{n:04x}"
        return {"id": pid, "criada_em": f"{dia:%Y-%m-%d}T{hora[:2]}:{hora[2:4]}:{hora[4:]}+00:00", "tarefa": "demo-email", "tipo": "email",
                "destino": destino, "resumo": assunto, "conteudo": corpo, "como_executar": "", "demo": True,
                "dados": {"assunto": assunto, "de": "Loja Exemplo <contato@loja.example.test>"}, "estado": estado,
                "historico": []}
    return [p(1, "pendente", "090000", "ana@exemplo.test", "Seu pedido está a caminho",
              "Oi, Ana. Seu caderno personalizado saiu hoje e chega em 3 dias úteis. (e-mail de exemplo)"),
            p(6, "feita", "143000", "caio@exemplo.test", "Novidades da semana",
              "Oi, Caio. Chegaram canetas novas na Loja Exemplo. (e-mail de exemplo)")]


def _recibo():
    return {"demo": True, "issue": "DEMO-1", "ok": True, "esperar": False, "faltas": [], "depende_fulano": False,
            "notas": ["exemplo-org/exemplo-loja#1: no ar (recibo de exemplo, nada foi conferido)"]}


# ---------- onde o painel lê ----------

def pasta_projetos_do_painel(d):
    """Onde o painel vai ler os projetos depois do demo em `d` (mesma ordem de hub/config.py: dado()):
    KRONOS_PROJETOS ou caminhos.projetos > <caminhos.dados>/projetos (existindo ou, aqui, criada pelo demo) > config.projetos()."""
    import os
    from hub import config
    explicito = os.environ.get("KRONOS_PROJETOS") or config.definido("caminhos.projetos")
    if explicito:
        return Path(explicito).expanduser()
    base = os.environ.get("KRONOS_CAMINHOS_DADOS") or config._carregado().get("caminhos", {}).get("dados")
    if base and Path(base).expanduser().resolve() == Path(d).resolve():
        return Path(d) / "projetos"
    return config.projetos()


# ---------- o demo.json não é confiável: só vale o que a própria demo geraria ----------

PADRAO = {"metas": re.compile(r"^m-\d{8}-(exemplo-loja-lcp|exemplo-clinica-nota|hub-conversoes)$"),
          "propostas": re.compile(r"^\d{8}-\d{6}-demo-email-[0-9a-f]{4}$"), "recibos": re.compile(r"^DEMO-1$")}


def _conteudo_exemplo(pid):
    """O JSON do projeto de exemplo `pid` (o que a demo grava), ou None se não é um dos exemplos."""
    for arq in _projetos():
        if arq.stem == pid:
            try:
                return json.loads(arq.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
    return None


def _projeto_da_demo(arq):
    """Projeto só é da demo se o conteúdo é IGUAL ao gerado a partir de exemplos/ (o arquivo de projeto não aceita marca)."""
    try:
        return arq.is_file() and json.loads(arq.read_text(encoding="utf-8")) == _conteudo_exemplo(arq.stem) is not None
    except (OSError, ValueError):
        return False


def _dia_ok(v):
    try:
        return dt.datetime.strptime(v, RE_DIA).date() if isinstance(v, str) else None
    except ValueError:
        return None


def _marca_valida(marca, log=print):
    """Cópia do demo.json só com o que a demo poderia ter escrito: negócios do PERFIL, de/ate datas com de<=ate, ids no padrão."""
    if not marca:
        return None
    v = dict(marca)
    bruto = marca.get("negocios") if isinstance(marca.get("negocios"), list) else []
    v["negocios"] = [n for n in bruto if isinstance(n, str) and n in PERFIL]
    if len(v["negocios"]) != len(bruto) or (bruto == [] and marca.get("negocios") not in (None, [])):
        log(f"aviso: negócios do {MARCADOR} que a demo não cria foram ignorados")
    de, ate = _dia_ok(marca.get("de")), _dia_ok(marca.get("ate"))
    if not (de and ate and de <= ate):
        if marca.get("de") is not None or marca.get("ate") is not None:
            log(f"aviso: intervalo de dias do {MARCADOR} inválido; nenhum fato será apagado")
        v["de"] = v["ate"] = None
    for chave, padrao in PADRAO.items():
        lista = marca.get(chave) if isinstance(marca.get(chave), list) else []
        v[chave] = [i for i in lista if isinstance(i, str) and padrao.match(i)]
        if len(v[chave]) != len(lista):
            log(f"aviso: ids de {chave} do {MARCADOR} que a demo não gera foram ignorados")
    lista = marca.get("projetos") if isinstance(marca.get("projetos"), list) else []
    v["projetos"] = [i for i in lista if isinstance(i, str) and i in {a.stem for a in _projetos()}]
    if len(v["projetos"]) != len(lista):
        log(f"aviso: projetos do {MARCADOR} que não são exemplos da demo foram ignorados")
    return v


# ---------- dado real = recusa ----------

def _reais(base, marca):
    """O que existe no diretório e NÃO é da demo (lista de descrições curtas, vazia = pode popular)."""
    import sqlite3
    from hub import armazem, config
    d = _dados(base)
    marca = _marca_valida(marca, lambda *a: None)
    meus = {k: {i for i in (marca or {}).get(k, [])} for k in ("projetos", "metas", "propostas")}
    achados = []
    pastas = {d / "projetos", pasta_projetos_do_painel(d)}  # onde a demo grava e onde o painel lê
    for pasta in sorted(pastas):
        if pasta.resolve() == (config.RAIZ / "exemplos" / "projetos").resolve():
            continue  # o padrão neutro do repositório não é dado de ninguém
        for arq in sorted(pasta.glob("*.json")):
            if arq.stem not in meus["projetos"] or not _projeto_da_demo(arq):
                achados.append(f"projeto {arq.name} em {pasta}")
    for sub, chave in (("claude/metas", "metas"), ("propostas", "propostas")):
        for arq in sorted((d / sub).glob("*.json")) if (d / sub).is_dir() else []:
            if arq.stem not in meus[chave] or not _marcado(arq):
                achados.append(f"{chave[:-1]} {arq.name} em {d / sub}")
    banco = armazem.caminho(d)
    if banco.is_file():
        ids = set((marca or {}).get("negocios", []))
        with closing(sqlite3.connect(banco.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as c:
            try:
                fora = [n for (n,) in c.execute("select distinct negocio from fato")]
            except sqlite3.DatabaseError:
                fora = ["(banco ilegível)"]
        achados += [f"fatos do BI do negócio {n}" for n in fora if n not in ids]
    return achados


# ---------- gravar / limpar ----------

def _tirar_fatos(base, marca, log=print):
    """O armazém não tem marca por fato: apaga só negócio demo + as 6 métricas da demo + o intervalo de dias da marca (dim vazio)
    E o valor igual ao que a semente da marca gera. Dado real gravado por cima da demo (valor diferente) fica."""
    from hub import armazem
    d = _dados(base)
    negocios, de, ate = marca.get("negocios", []), marca.get("de"), marca.get("ate")
    if not (negocios and isinstance(de, str) and isinstance(ate, str)) or not armazem.caminho(d).is_file():
        return
    negocios = [n for n in negocios if isinstance(n, str)]
    try:
        fim = dt.datetime.strptime(ate, RE_DIA).date()
        esperado = serie(marca.get("semente", SEMENTE), fim, marca.get("dias", DIAS))
    except (ValueError, TypeError):
        return  # marca que não dá para reproduzir: não apaga fato nenhum
    with closing(armazem.abrir(d)) as c:
        c.executemany("delete from fato where negocio=? and fonte=? and metrica=? and dim='' and dia=? and valor=?",
                      [(f["negocio"], f["fonte"], f["metrica"], f["dia"], f["valor"]) for f in esperado
                       if f["negocio"] in negocios and de <= f["dia"] <= ate])
        c.commit()


def _gravar_json(arq, obj):
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_name(f".{arq.name}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(arq)


def popular(base=None, semente=SEMENTE, fim=None, log=print):
    """Devolve 0 (feito) ou 1 (recusado, com a explicação em stderr).
    Grava antes um demo.json provisório (`completo: false`) com tudo o que vai criar: se algo falhar no meio, `demo` de novo
    ou `demo --limpar` tratam o que ficou como demo (a recusa de dado real continua valendo para o que não está na lista)."""
    from hub import armazem, metas, propostas, sites
    d = _dados(base)
    marca = _marca_valida(_ler_marca(d))
    reais = _reais(d, marca)
    if reais:
        print(f"kronos demo: recusei: {d} já tem dado sem a marca de demonstração ({len(reais)}):", file=sys.stderr)
        for r in reais[:8]:
            print(f"  - {r}", file=sys.stderr)
        print("A demo só roda em um diretório de dados vazio (ou que só tem demo). Use --dados com uma pasta nova.", file=sys.stderr)
        return 1
    fim = fim or dt.date.today()
    arqs = _projetos()
    for arq in arqs:  # valida antes de gravar qualquer coisa
        _, erros = sites._ler(arq)
        if erros:
            print("kronos demo: exemplo inválido: " + "; ".join(erros), file=sys.stderr)
            return 1
    ms, ps, fatos = _metas(fim), _propostas(fim), serie(semente, fim)
    for p in ps:
        propostas.validar(p, d)
    banco_novo = bool((marca or {}).get("banco_novo")) or not armazem.caminho(d).exists()
    if marca:
        limpar(d, log=lambda *a: None)  # rodar de novo troca tudo (metas e propostas levam a data): nada do 1º run fica órfão
        banco_novo = banco_novo or not armazem.caminho(d).exists()
    nova = {"demo": True, "completo": False, "semente": semente, "dias": DIAS, "de": min(f["dia"] for f in fatos),
            "ate": max(f["dia"] for f in fatos), "projetos": [a.stem for a in arqs], "negocios": sorted({f["negocio"] for f in fatos}),
            "metas": [m["id"] for m in ms], "propostas": [p["id"] for p in ps], "recibos": ["DEMO-1"], "banco_novo": banco_novo}
    _gravar_json(marcador(d), nova)
    for arq in arqs:
        _gravar_json(d / "projetos" / arq.name, json.loads(arq.read_text(encoding="utf-8")))
    armazem.gravar(fatos, d)
    for m in ms:
        metas.gravar(m, d)
    for p in ps:
        propostas._gravar(p, d)
    _gravar_json(d / "demo" / "recibos" / "DEMO-1.json", _recibo())
    _gravar_json(marcador(d), {**nova, "completo": True})
    log(f"demo: {len(arqs)} projetos, {len(fatos)} fatos de {DIAS} dias (semente {semente}), {len(ms)} metas, {len(ps)} propostas, 1 recibo em {d}")
    lido = pasta_projetos_do_painel(d)
    if lido.resolve() != (d / "projetos").resolve():
        print(f"aviso: o painel lê os projetos de {lido}, não de {d / 'projetos'}: a demo só aparece lá se você apontar para a mesma pasta.", file=sys.stderr)
    log("Reinicie o painel para ele ler os projetos novos: docker compose restart painel")
    return 0


RE_ID = re.compile(r"^[\w.-]+$")


def _caminhos(pasta, ids, sufixo, permitidos=None, log=print):
    """Arquivos <id><sufixo> dentro de `pasta`, só de id válido (texto, [A-Za-z0-9_.-]+, nem '.' nem '..', na lista `permitidos` se houver)
    cujo alvo resolvido fica dentro de `pasta`. O resto é ignorado com aviso (nunca TypeError, nunca fora da pasta)."""
    raiz = Path(pasta).resolve()
    saida = []
    for i in ids if isinstance(ids, list) else []:
        if not isinstance(i, str) or not RE_ID.match(i) or set(i) == {"."} or (permitidos is not None and i not in permitidos):
            log(f"aviso: id {i!r} do {MARCADOR} ignorado (não é um id da demo)")
            continue
        alvo = (raiz / f"{i}{sufixo}").resolve()
        if alvo.parent != raiz:
            log(f"aviso: id {i!r} do {MARCADOR} ignorado (fora de {raiz})")
            continue
        saida.append(alvo)
    return saida


def _marcado(arq):
    try:
        return json.loads(arq.read_text(encoding="utf-8")).get("demo") is True
    except (OSError, ValueError, AttributeError):
        return False


def limpar(base=None, log=print):
    """Remove só o que a marca lista, valida cada id e confere a marca no arquivo (menos projeto: só id fixo dos exemplos)."""
    from hub import armazem, metas, propostas
    d = _dados(base)
    marca = _marca_valida(_ler_marca(d), log)
    if not marca:
        log(f"demo: nada marcado como demonstração em {d}")
        return 0
    fixos = {a.stem for a in _projetos()}  # projeto não carrega a marca no arquivo: só os ids dos exemplos da demo valem
    for arq in _caminhos(d / "projetos", marca.get("projetos"), ".json", fixos, log):
        if _projeto_da_demo(arq):
            arq.unlink()
        else:
            log(f"aviso: {arq.name} não é igual ao exemplo da demo; fica")
    for pasta, chave in ((metas.pasta(d), "metas"), (propostas.pasta(d), "propostas"), (d / "demo" / "recibos", "recibos")):
        for arq in _caminhos(pasta, marca.get(chave), ".json", None, log):
            if _marcado(arq):
                arq.unlink()
    _tirar_fatos(d, marca)
    banco = armazem.caminho(d)
    if marca.get("banco_novo") and banco.is_file():
        import sqlite3
        with closing(sqlite3.connect(banco)) as c:
            vazio = c.execute("select count(*) from fato").fetchone()[0] == 0
        if vazio:
            banco.unlink()
    marcador(d).unlink(missing_ok=True)
    for pasta in (d / "demo" / "recibos", d / "demo", banco.parent, metas.pasta(d), d / "claude", propostas.pasta(d), d / "projetos"):
        try:
            pasta.rmdir()  # só se ficou vazia (nunca apaga o que tem dado)
        except OSError:
            pass
    log(f"demo: dados de demonstração removidos de {d}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kronos demo", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dados", metavar="DIR", help="diretório de dados (padrão: o do painel)")
    ap.add_argument("--semente", type=int, default=SEMENTE, help=f"semente da série inventada (padrão {SEMENTE})")
    ap.add_argument("--fim", metavar="AAAA-MM-DD", help="último dia da série (padrão: hoje)")
    ap.add_argument("--limpar", action="store_true", help="remove só o que está marcado como demonstração")
    a = ap.parse_args(argv)
    try:
        fim = dt.date.fromisoformat(a.fim) if a.fim else None
    except ValueError:
        ap.error("--fim: use AAAA-MM-DD")
    if a.limpar:
        return limpar(a.dados)
    return popular(a.dados, a.semente, fim)


if __name__ == "__main__":
    sys.exit(main())
