"""Alertas que importam: site fora do ar, deploy travado, cliente da fsite05 esperando no WhatsApp e conta Claude perto do
limite da semana. O que o orquestrador pode consertar vai primeiro a ele; o dono só recebe se continuar, no máximo
1 aviso por dia por item. Queda de receita/leads/cliques fica no hub/bi_leitura.vigiar (não duplicar). Root, cron de 2 min.

    python3 -m hub.alertas               checar, chamar o orquestrador e avisar
    python3 -m hub.alertas --sem-avisar  só imprime o que acharia (não grava estado, não chama ninguém)
"""
import concurrent.futures
import datetime as dt
import http.client
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from hub import conta, data, saude, sites, faltou
from hub.conta import BRT, de_iso, iso
try:
    from hub.vigia_clientej import CMD_ORQ
except ImportError as _e:  # snapshot público: sem o plugin não há comando do orquestrador; chamar_orq devolve a falha
    if not faltou(_e, "hub.vigia_clientej"):
        raise
    CMD_ORQ = None

ESPERA = {"site": 5 * 60}  # segundos fora antes de agir; demais tipos 0
DEPLOY_MIN = 30
DEPLOYS_VELHO_MIN = 45  # saude.json renova os deploys a cada ~30 min; foto mais velha que isso não vale
ORQ_TIPOS = {"site", "deploy"}
CONTA_PCT = 85
HORARIO = {0: (8, 18), 1: (8, 18), 2: (8, 18), 3: (8, 18), 4: (8, 18), 5: (8, 12)}  # BRT, seg=0; domingo fora
CLIENTE_A_CADA = 10 * 60
TIMEOUT_ORQ = 15 * 60
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


def pasta(base_dados=None):
    return Path(base_dados or data()) / "alertas"


def _achado(chave, tipo, item, texto, orch):
    return {"chave": chave, "tipo": tipo, "item": item, "texto": texto, "orch": orch}


# ---- regras (puras) ----

def achados_sites(dominios, http_get):
    """http_get(url) -> status HTTP ou None (sem resposta). Em paralelo: 5 sites x 20 s não cabem nos 2 min."""
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        status = list(ex.map(lambda d: http_get(f"https://{d}/"), dominios))
    return [_achado(f"site:{d}", "site", d, f"Site {d} fora do ar ({'sem resposta' if s is None else f'erro {s}'})", True)
            for d, s in zip(dominios, status) if s is None or s >= 500]


def achados_deploys(saude_, agora):
    """Mede `deploys_em - em` (até quando a foto viu rodando), não `agora - em`: a foto é de até 30 min atrás."""
    foto = (saude_ or {}).get("deploys_em")
    if not foto or (agora - de_iso(foto)).total_seconds() > DEPLOYS_VELHO_MIN * 60:
        return []
    saida = []
    for d in saude_.get("deploys") or []:
        if d.get("status") != "running" or not d.get("em"):
            continue
        minutos = int((de_iso(foto) - de_iso(d["em"])).total_seconds() // 60)
        if minutos > DEPLOY_MIN:
            nome = d.get("appName") or d.get("app")
            saida.append(_achado(f"deploy:{nome}", "deploy", nome,
                                 f"Deploy do {d.get('projeto')}/{d.get('app')} travado (rodando há {minutos} min)", True))
    return saida


def horario_comercial(agora):
    b = agora.astimezone(BRT)
    ini, fim = HORARIO.get(b.weekday(), (0, 0))
    return ini <= b.hour < fim


def achados_clientes(conversas, agora):
    """conversas já triadas: somente entradas que pedem resposta. Sem dados pessoais no achado."""
    if not conversas:
        return []
    antigo = min(conversas, key=lambda c: de_iso(c["ultima_em"]))["ultima_em"]
    return [_achado("cliente:fsite05", "cliente", "fsite05",
                    f"fsite05: {len(conversas)} cliente(s) esperando resposta no WhatsApp há mais de 2 h "
                    f"(o mais antigo desde {_hora(antigo, agora)})", False)]


def achados_contas(uso):
    contas = [(n, c) for n, c in (uso.get("contas") or {}).items()] + [("orquestrador", uso.get("orquestrador"))]
    saida = []
    for n, c in contas:
        semana = (c or {}).get("semana") or {}
        if semana.get("pct") is None or semana["pct"] < CONTA_PCT:
            continue
        volta = f" (volta {de_iso(semana['reset']).astimezone(BRT):%d/%m %H:%M})" if semana.get("reset") else ""
        saida.append(_achado(f"conta:{n}", "conta", n, f"Conta Claude {n} em {round(semana['pct'])}% do limite da semana{volta}",
                             False))
    return saida


# ---- decisão (pura) ----

def decidir(achados, estado, agora):
    """-> (acoes [(\"orch\"|\"avisar\", chave)], novo_estado). Chave que sumiu dos achados sai do estado sem aviso.
    `avisados` e `orq_chamados` (por dia BRT, fora dos itens) seguram o pisca-pisca: 1 orch e 1 aviso por chave por dia."""
    hoje = f"{agora.astimezone(BRT):%Y-%m-%d}"
    estado = estado or {}
    antigos, itens, acoes = estado.get("itens") or {}, {}, []
    avisados = {c: d for c, d in (estado.get("avisados") or {}).items() if d == hoje}
    orq_chamados = {c: d for c, d in (estado.get("orq_chamados") or {}).items() if d == hoje}
    for a in achados:
        it = {"desde": iso(agora), "orq_em": None, "orq_o_que": None, "avisado_dia": None, **antigos.get(a["chave"], {}),
              "tipo": a["tipo"], "item": a["item"], "texto": a["texto"]}
        itens[a["chave"]] = it
        if (agora - de_iso(it["desde"])).total_seconds() < ESPERA.get(a["tipo"], 0):
            continue
        if a["orch"] and not it["orq_em"] and orq_chamados.get(a["chave"]) != hoje:
            acoes.append(("orch", a["chave"]))
            it["orq_em"] = iso(agora)
            orq_chamados[a["chave"]] = hoje
            continue
        if a["orch"] and it["orq_em"] and not it["orq_o_que"] and (agora - de_iso(it["orq_em"])).total_seconds() > TIMEOUT_ORQ + 300:
            it["orq_o_que"] = "o orquestrador não conseguiu agir (a chamada não terminou)"  # processo morreu no meio
        # voltou no mesmo dia depois do orch: segue para o aviso sem chamar de novo
        orq_feito = it["orq_o_que"] or (not it["orq_em"] and orq_chamados.get(a["chave"]) == hoje)
        if a["tipo"] == "cliente":
            continue  # operacional: painel + crm_tarefas, nunca caixa/WhatsApp do dono
        if (not a["orch"] or orq_feito) and avisados.get(a["chave"]) != hoje:
            acoes.append(("avisar", a["chave"]))  # a marca do aviso só entra depois de enviado (executar)
    return acoes, {"cliente_checado_em": None, **estado, "itens": itens, "avisados": avisados, "orq_chamados": orq_chamados}


def _hora(desde, agora):
    d, hoje = de_iso(desde).astimezone(BRT), agora.astimezone(BRT)
    return f"{d:%H:%M}" + ("" if d.date() == hoje.date() else f" de {d:%d/%m}")


def prompt_orq(it, agora):
    return (f"Alerta do hub: {it['texto']}, desde {_hora(it['desde'], agora)} (Brasília). Investigue e conserte se puder "
            "(ssh vps-servidor_a / servidor_hub, Dokploy, docker...). Não mande mensagem a ninguém. "
            "Responda só 1–2 frases: o que achou e o que fez.")


def mensagem(it, agora):
    desde = "" if it.get("tipo") == "cliente" else f", desde {_hora(it['desde'], agora)}"  # o texto do cliente já traz a hora
    return f"{it['texto']}{desde}." + (f" Orquestrador: {it['orq_o_que']}" if it.get("orq_o_que") else "")


# ---- efeitos (injetáveis nos testes) ----

def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (OSError, http.client.HTTPException, ValueError):  # rede, TLS, timeout
        return None


def consultar_crm(agora):
    from hub.apps import Rest, env_do_conteiner
    env = env_do_conteiner("cliente-a-site")
    z = lambda t: t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    rest = Rest(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    conversas = rest.get(
        "crm_conversas", 1000, select="id,contato_id,nome_wa,atribuida_a,ultima_em", ultima_direcao="eq.in", arquivada="eq.false",
        **{"and": f"(ultima_em.gte.{z(agora - dt.timedelta(days=3))},ultima_em.lte.{z(agora - dt.timedelta(hours=2))})"},
        order="ultima_em.asc", canal="eq.whatsapp")
    for c in conversas:
        mensagens = rest.get("crm_mensagens", 1, select="tipo,texto,bruto,direcao,criado_em",
                             conversa_id="eq." + c["id"], interna="eq.false", order="criado_em.desc")
        # Reconfere saída concorrente: nunca atribui o tipo de uma entrada antiga à última mensagem.
        c["mensagem"] = mensagens[0] if mensagens and mensagens[0]["direcao"] == "in" else {}
        contatos = rest.get("crm_contatos", 1, select="nome,opt_out", id="eq." + c["contato_id"]) if c.get("contato_id") else []
        c["opt_out"] = bool(contatos and contatos[0].get("opt_out"))
        c["nomes"] = [n for n in (c.pop("nome_wa", None), contatos[0].get("nome") if contatos else None) if n]
    return conversas


def fila_clientes(conversas, descadastros, agora):
    from hub.apps import Rest, env_do_conteiner
    try:
        from hub.clientes_esperando import fila
    except ImportError as e:  # snapshot público: sem a triagem de clientes, não há fila
        if not faltou(e, "hub.clientes_esperando"):
            raise
        return
    env = env_do_conteiner("cliente-a-site")
    fila(Rest(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"]), conversas, descadastros, agora)


def chamar_orq(texto):
    falha = "o orquestrador não conseguiu agir ({})"
    if CMD_ORQ is None:
        return falha.format("comando do orquestrador ausente")
    try:
        r = subprocess.run(CMD_ORQ, input=texto, capture_output=True, text=True, timeout=TIMEOUT_ORQ)
    except subprocess.TimeoutExpired:
        return falha.format("demorou mais de 15 min")
    except OSError as e:
        return falha.format(type(e).__name__)
    if r.returncode or not r.stdout.strip():
        return falha.format(f"código {r.returncode}")
    return r.stdout.strip()[-300:]


def ler_estado(arq):
    try:
        estado = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return estado if isinstance(estado, dict) else {}


def salvar_estado(arq, estado):
    arq.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=arq.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o644)
        os.replace(tmp, arq)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def anteriores(estado, tipo):
    """Achados do tipo como estavam no estado: fonte com erro não pode fazer o item sumir (e voltar com aviso novo)."""
    return [_achado(c, tipo, it["item"], it["texto"], tipo in ORQ_TIPOS)
            for c, it in (estado.get("itens") or {}).items() if it.get("tipo") == tipo]


def _sites(http_get):
    dominios = [s["dominio"] for s in sites.ativos()]
    if not dominios:
        raise ValueError("cadastro de sites vazio")
    return achados_sites(dominios, http_get)


def _cliente(estado, agora, consultar, sem_avisar=False):
    # A asite01 não tem CRM de conversa: só a fsite05.
    if not horario_comercial(agora):
        return []
    velho = anteriores(estado, "cliente") if estado.get("cliente_triagem_v1") else []
    feito = estado.get("cliente_checado_em")
    if feito and (agora - de_iso(feito)).total_seconds() < CLIENTE_A_CADA - 30:
        return velho
    try:
        if consultar is consultar_crm:  # consulta injetada (testes) já vem triada: não precisa da triagem
            try:
                from hub.clientes_esperando import selecionar
            except ImportError as e:  # snapshot público: sem a triagem de clientes, a regra do CRM não acha nada
                if not faltou(e, "hub.clientes_esperando"):
                    raise  # dependência quebrada: cai no except abaixo (log e achado anterior), como antes
                return []
        conversas = consultar(agora)
        # Consultas injetadas nos testes podem representar a saída já triada.
        if consultar is consultar_crm:
            cache = estado.setdefault("cliente_classificacoes", {})
            conversas, descadastros = selecionar(conversas, cache)
            if not sem_avisar:
                fila_clientes(conversas, descadastros, agora)
        achados = achados_clientes(conversas, agora)
    except Exception as e:  # noqa: BLE001 — CRM fora: mantém o achado anterior e tenta na rodada seguinte
        print(f"falha ao checar cliente: {type(e).__name__}")
        return velho
    estado["cliente_triagem_v1"] = True
    estado["cliente_checado_em"] = iso(agora)
    return achados


def coletar(estado, agora, base, http_get=http_get, consultar_crm=consultar_crm, sem_avisar=False):
    """Cada regra que falha por erro próprio só vai ao log; as outras seguem."""
    regras = (("site", lambda: _sites(http_get)),
              ("deploy", lambda: achados_deploys(saude.ler(base), agora)),
              ("cliente", lambda: _cliente(estado, agora, consultar_crm, sem_avisar)),
              ("conta", lambda: achados_contas(json.loads((Path(base) / "claude" / "uso-contas.json").read_text(encoding="utf-8")))))
    achados = []
    for nome, regra in regras:
        try:
            achados += regra()
        except Exception as e:  # noqa: BLE001 — qualquer falha de uma fonte não pode derrubar as outras
            print(f"falha ao checar {nome}: {type(e).__name__}: {str(e)[:200]}")
            achados += anteriores(estado, nome)
    return achados


def _avisar_caixa(msg, urgente):
    conta.avisar(msg, agora_=urgente, origem="alertas", tipo="tecnico")  # site, deploy, cliente, conta: do orquestrador


def executar(acoes, estado, agora, salvar, chamar_orq=chamar_orq, avisar=_avisar_caixa):
    """avisar(msg, urgente). Avisos antes do orch (chamada longa não atrasa aviso); o aviso só é marcado depois de enviado,
    o orch já vem marcado do decidir (morte no meio não o chama de novo a cada 2 min). Falha numa ação não para as outras."""
    codigo = 0
    hoje = f"{agora.astimezone(BRT):%Y-%m-%d}"
    for tipo, chave in sorted(acoes, key=lambda a: a[0] != "avisar"):
        it = estado["itens"][chave]
        try:
            if tipo == "orch":
                it["orq_o_que"] = chamar_orq(prompt_orq(it, agora))
                print(f"orquestrador ({chave}): {it['orq_o_que']}")
            else:
                if it["tipo"] in ORQ_TIPOS and avisar is _avisar_caixa:
                    from hub.debugger import alerta_tecnico
                    alerta_tecnico(mensagem(it, agora))
                else:
                    avisar(mensagem(it, agora), it["tipo"] != "conta")  # conta não é urgente: respeita o horário do bot
                it["avisado_dia"] = estado.setdefault("avisados", {})[chave] = hoje
                print(f"avisado ({chave})")
        except Exception as e:  # noqa: BLE001
            print(f"falha em {tipo} ({chave}): {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            if tipo == "orch":
                it["orq_em"] = None
                estado.get("orq_chamados", {}).pop(chave, None)
            codigo = 1
        salvar(estado)
    return codigo


def rodar(base, agora, sem_avisar=False, http_get=http_get, consultar_crm=consultar_crm, chamar_orq=chamar_orq,
          avisar=_avisar_caixa):
    arq = pasta(base) / "estado.json"
    estado = ler_estado(arq)
    estado.pop("_sem_avisar", None)
    achados = coletar(estado, agora, base, http_get, consultar_crm, sem_avisar)
    acoes, estado = decidir(achados, estado, agora)
    if sem_avisar:
        for a in achados:
            print(f"achado {a['chave']}: {a['texto']}")
        print(f"ações: {acoes or 'nenhuma'}")
        return 0
    salvar = lambda e: salvar_estado(arq, e)  # noqa: E731
    salvar(estado)  # antes de agir: marca do orch gravada, se ele travar não é chamado de novo a cada 2 min
    return executar(acoes, estado, agora, salvar, chamar_orq, avisar)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    return rodar(data(), conta.agora(), sem_avisar="--sem-avisar" in argv)


if __name__ == "__main__":
    raise SystemExit(main())
