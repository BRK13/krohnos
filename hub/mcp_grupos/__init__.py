"""Grupos de ferramentas do MCP do hub: um módulo por grupo nesta pasta, achado sozinho (sem lista para editar).

Cada módulo define:
    FERRAMENTAS = {nome: {"description": ..., "inputSchema": {...}}}
    def executar(nome, args, site) -> dict    # site: cadastro de sites/<id>.json, ou None se o pedido não trouxe site

Pedido errado do cliente: levante ErroUso("mensagem"), que vira isError. Falha de fonte externa: devolva
{"ok": False, "reason": ..., ...}, como as ferramentas do núcleo (hub/mcp_proxy.py). Os ajudantes abaixo
são os mesmos que o núcleo usa, mais pasta(grupo, site_id) para guardar estado: chamada paga ao DataForSEO com custo, local e idioma padrão, números e listas.
"""
import importlib
import pkgutil

from hub import data, dataforseo
from hub.config import dono
from hub import sites as S


class ErroUso(ValueError):
    """Pedido errado do cliente: vira isError com esta mensagem."""


def juntar(modulos):
    """{nome da ferramenta: módulo}. Nome repetido entre grupos é erro de programação: falha alto ao carregar."""
    dono = {}
    for m in modulos:
        for nome in m.FERRAMENTAS:
            if nome in dono:
                raise RuntimeError(f"ferramenta {nome} definida em {dono[nome].__name__} e em {m.__name__}")
            dono[nome] = m
    return dono


def carregar():
    mods = []
    for i in pkgutil.iter_modules(__path__):
        if i.name.startswith("_"):
            continue
        # importar o módulo grava pacote.<nome>: se o nome já é um ajudante (ex.: local), o ajudante some para quem importar depois
        ja = globals().get(i.name)
        if ja is not None and getattr(ja, "__name__", None) != f"{__name__}.{i.name}":
            raise RuntimeError(f"o grupo {i.name} esconde hub.mcp_grupos.{i.name}: renomeie o módulo")
        mods.append(importlib.import_module(f"{__name__}.{i.name}"))
    return juntar(mods)


def pasta(grupo, site_id=None):
    """Pasta gravável do grupo em $HUB_DATA/mcp/<grupo>[/<site>] (a única que o serviço pode gravar). Cria se faltar."""
    p = data() / "mcp" / grupo / (site_id or "")
    p.mkdir(parents=True, exist_ok=True)
    return p


def inteiro(args, chave, padrao, maximo):
    try:
        return max(1, min(int(args.get(chave) or padrao), maximo))
    except (TypeError, ValueError):
        raise ErroUso(f"{chave} tem que ser um número") from None


def padrao():
    p = S.config_hub()["dataforseo_padrao"]
    return p["locationCode"], p["languageCode"]


def local(args):
    loc, lang = padrao()
    return {"location_code": args.get("location_code") or args.get("locationCode") or loc,
            "language_code": args.get("language_code") or args.get("languageCode") or lang}


def palavras(args, maximo):
    k = args.get("keywords")
    if not isinstance(k, list) or not k or not all(isinstance(x, str) and x.strip() for x in k):
        raise ErroUso("keywords tem que ser uma lista de palavras")
    if len(k) > maximo:
        raise ErroUso(f"no máximo {maximo} palavras por chamada")
    return k


def alvo(args, site):
    a = next((args[k] for k in ("target", "domain", "dominio") if args.get(k)), None) or (site and site["dominio"])
    if not a:
        raise ErroUso("informe target (domínio) ou site")
    return a


def so(d, chaves):
    return {k: d.get(k) for k in chaves}


def _tarefa(caminho, tarefa, extrair, montar):
    try:
        tarefas = dataforseo.post(caminho, [tarefa])
    except RuntimeError as e:  # a mensagem do cliente já vem sem a chave
        return {"ok": False, "reason": "dataforseo_erro", "message": str(e)}
    except (OSError, KeyError, ValueError):
        return {"ok": False, "reason": "dataforseo_erro", "message": f"chave do DataForSEO não configurada no hub; avise o {dono()}"}
    custo = round(sum(t.get("cost") or 0 for t in tarefas), 6)
    t = tarefas[0] if tarefas else {}
    if t.get("status_code") != 20000:
        return {"ok": False, "reason": "dataforseo_erro", "message": f"DataForSEO {t.get('status_code')}: {t.get('status_message')}", "custo": custo}
    return {"ok": True, **montar(extrair(t.get("result") or [])), "custo": custo}


def dfs(caminho, tarefa, montar):
    """Uma tarefa live do DataForSEO. montar(itens) -> dict; a resposta leva ok, custo (US$) e o que montar devolveu."""
    return _tarefa(caminho, tarefa, lambda res: [i for r in res for i in (r or {}).get("items") or []], montar)


def dfs_resultado(caminho, tarefa, montar):
    """Como dfs, para endpoint que devolve o dado direto em result[] (ex.: backlinks/summary): montar(result) recebe a lista result."""
    return _tarefa(caminho, tarefa, list, montar)
