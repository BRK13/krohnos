"""explore_prompt: uma pergunta avulsa a até 4 modelos pela API ao vivo do DataForSEO, com resposta, citações e menção da marca.

Cache de 7 dias por modelo em pasta("visibilidade_ia", <site>)/cache/<hash>.json: resposta guardada não custa de novo.
highlightBrand fica fora da chave do cache e é aplicada a cada leitura.
"""
import datetime as dt
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from hub import citacao, dataforseo
from hub.mcp_grupos import ErroUso, pasta
from hub.mcp_grupos.visibilidade_ia import _agora, _f, _iso
from hub.mcp_grupos.visibilidade_ia_rodada import MODELO

MODELOS = ["chat_gpt", "claude", "gemini", "perplexity"]
ROTULO = {"chat_gpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini", "perplexity": "Perplexity"}
# shortcut: o original escolhe o modelo pelo catálogo grátis do DataForSEO (GET); aqui ficam o fixado e o primeiro do fallback dele.
NOME_MODELO = {"chat_gpt": "gpt-5.6-luna", "gemini": "gemini-2.5-pro", "claude": MODELO["claude"], "perplexity": MODELO["perplexity"]}
VALIDADE = dt.timedelta(days=7)
VERSAO_CACHE = 7
PAISES = """AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF
CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN
GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR
LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG
PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL
TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW""".split()
PAISES_CLAUDE = set("""AR AT AU BE BR CA CH CL CN DE DK ES FI FR GB HK ID IN IT JP KR MX MY NL NO NZ PH PL PT RU SA SE TR TW US
ZA""".split())
ACEITA_PAIS = {"chat_gpt": set(PAISES), "claude": PAISES_CLAUDE, "gemini": set(), "perplexity": set(PAISES)}

FERRAMENTAS = {
    "explore_prompt": _f(
        "Faz uma pergunta no ChatGPT, ou compara as respostas de modelos escolhidos. Devolve o texto da resposta, citações, buscas "
        "feitas, menção da marca e erro por modelo. Use para uma comparação avulsa; para observar o site de consumo e acompanhar com o "
        "tempo, use as ferramentas de rastreamento em IA. Cobra o uso real de cada modelo sem cache; com busca na web pode fazer uma "
        "tentativa paga a mais se a primeira resposta não buscou. Respostas guardadas são de graça por sete dias. Não configura "
        "rastreamento. Custa crédito do DataForSEO.",
        {"prompt": {"type": "string", "minLength": 1, "maxLength": 500,
                    "description": "A pergunta exata, até 500 caracteres. Mantenha as palavras do usuário."},
         "models": {"type": "array", "items": {"type": "string", "enum": MODELOS}, "minItems": 1, "maxItems": 4, "default": ["chat_gpt"],
                    "description": "Modelos: chat_gpt, claude, gemini, perplexity. Padrão: só o ChatGPT. Passe uma lista para comparar; "
                                   "cada modelo sem cache gasta crédito."},
         "highlightBrand": {"type": "string", "minLength": 1, "maxLength": 250,
                            "description": "Marca opcional para destacar no texto e nas citações. Não muda a pergunta enviada."},
         "webSearch": {"type": "boolean", "default": True,
                       "description": "Permite busca na web (padrão true). A resposta diz se o modelo buscou de fato; permitir não "
                                      "garante citações."},
         "webSearchCountryCode": {"type": "string", "enum": PAISES,
                                  "description": "País da busca na web, duas letras. Omita para nenhuma preferência. O Gemini não "
                                                 "aceita país; combinação de modelo e país sem suporte volta como erro do modelo, "
                                                 "sem chamada paga."}},
        ["prompt"]),
}


def _validar(args):
    prompt = args.get("prompt")
    prompt = prompt.strip() if isinstance(prompt, str) else ""
    if not 1 <= len(prompt) <= 500:
        raise ErroUso("prompt tem que ter de 1 a 500 caracteres")
    modelos = args.get("models", ["chat_gpt"])
    if not isinstance(modelos, list) or not 1 <= len(modelos) <= 4 or any(m not in MODELOS for m in modelos):
        raise ErroUso(f"models é uma lista de 1 a 4 entre: {', '.join(MODELOS)}")
    marca = args.get("highlightBrand")
    if marca is not None and not (isinstance(marca, str) and 1 <= len(marca.strip()) <= 250):
        raise ErroUso("highlightBrand tem que ter de 1 a 250 caracteres")
    busca = args.get("webSearch", True)
    if not isinstance(busca, bool):
        raise ErroUso("webSearch tem que ser true ou false")
    pais = args.get("webSearchCountryCode")
    if pais is not None and pais not in PAISES:
        raise ErroUso("webSearchCountryCode tem que ser um código de país de duas letras (ISO 3166-1, ex. BR)")
    return prompt, list(dict.fromkeys(modelos)), marca and marca.strip(), busca, pais


def _url_segura(u):
    try:
        p = urlsplit(u)
        return u if p.scheme in ("http", "https") and p.hostname and not p.username and not p.password else None
    except (TypeError, ValueError, AttributeError):
        return None


def _formatar(modelo, r, pais):
    """Resposta sem a marca (é o que vai para o cache)."""
    secoes = [s for i in r.get("items") or [] if isinstance(i, dict) and i.get("type") == "message"
              for s in i.get("sections") or [] if isinstance(s, dict)]
    citacoes, vistas = [], set()
    for a in (a for s in secoes for a in s.get("annotations") or [] if isinstance(a, dict)):
        u = _url_segura(a.get("url"))
        if u and u not in vistas:
            vistas.add(u)
            citacoes.append({"url": u, "domain": re.sub(r"^www\.", "", urlsplit(u).hostname), "title": a.get("title"), "matchedBrand": False})
    tokens = r.get("output_tokens")
    return {"status": "success", "model": modelo, "modelName": r.get("model_name"),
            "text": "\n\n".join(s["text"] for s in secoes if isinstance(s.get("text"), str) and s["text"]).strip(),
            "citations": citacoes[:25], "fanOutQueries": (r.get("fan_out_queries") or [])[:20], "brandMentioned": None,
            "outputTokens": None if tokens is None else int(tokens + 0.5), "webSearch": bool(r.get("web_search")),
            "webSearchCountryCode": pais}


def _com_marca(res, marca):
    if res["status"] != "success":
        return res
    cits = [{**c, "matchedBrand": bool(marca) and marca.lower() in f"{c['url']} {c['title'] or ''}".lower()} for c in res["citations"]]
    # menção pela regra do hub (palavra inteira, sem acento nem maiúscula), no lugar da regex do original
    mencionada = None if not marca else any(c["matchedBrand"] for c in cits) or bool(citacao.trechos(res["text"], [marca]))
    return {**res, "citations": cits, "brandMentioned": mencionada}


def _erro(modelo, codigo, msg):
    return {"status": "error", "model": modelo, "errorCode": codigo, "message": msg}


def _pedir(modelo, prompt, busca, pais):
    """(resultado bruto ou None, erro, custo) de uma chamada ao vivo."""
    corpo = {"user_prompt": prompt, "model_name": NOME_MODELO[modelo], "web_search": busca,
             **({"force_web_search": True} if busca and modelo == "claude" else {}), "max_output_tokens": 4096,
             **({"web_search_country_iso_code": pais} if pais else {})}
    try:
        tarefas = dataforseo.post(f"ai_optimization/{modelo}/llm_responses/live", [corpo], timeout=150)
    except (RuntimeError, OSError, KeyError, ValueError) as e:
        return None, str(e) or type(e).__name__, 0
    t = tarefas[0] if tarefas else {}
    r = (t.get("result") or [None])[0]
    if t.get("status_code") != 20000 or not isinstance(r, dict):
        return None, t.get("status_message") or f"DataForSEO {t.get('status_code')}", t.get("cost") or 0
    return r, None, t.get("cost") or 0


def _cache(site, modelo, prompt, busca, pais):
    chave = json.dumps({"model": modelo, "modelName": NOME_MODELO[modelo], "prompt": re.sub(r"\s+", " ", prompt), "webSearch": busca,
                        "webSearchCountryCode": pais, "v": VERSAO_CACHE}, sort_keys=True, ensure_ascii=False)
    d = pasta("visibilidade_ia", site["id"]) / "cache"
    d.mkdir(exist_ok=True)
    return d / f"{hashlib.sha256(chave.encode()).hexdigest()}.json"


def _um(site, modelo, prompt, busca, pais_pedido, marca):
    """(resultado do modelo, custo)."""
    pais = pais_pedido if busca else None
    if pais and pais not in ACEITA_PAIS[modelo]:
        motivo = f"{ROTULO[modelo]} não aceita escolher país." if modelo == "gemini" else f"{ROTULO[modelo]} não aceita {pais} como país da busca."
        return _erro(modelo, "UNSUPPORTED_COUNTRY", f"{motivo} Tire webSearchCountryCode e rode de novo para incluir {ROTULO[modelo]}."), 0
    arq = _cache(site, modelo, prompt, busca, pais)
    try:
        guardado = json.loads(arq.read_text(encoding="utf-8"))
        if _agora() - dt.datetime.fromisoformat(guardado["salvoEm"].replace("Z", "+00:00")) < VALIDADE:
            return _com_marca(guardado["resultado"], marca), 0
    except (OSError, ValueError, KeyError, TypeError):
        pass
    custo = 0
    try:  # como o Promise.allSettled do original: qualquer erro vira o resultado deste modelo, com o custo já pago
        r, erro, custo = _pedir(modelo, prompt, busca, pais)
        if erro:
            return _erro(modelo, "UPSTREAM_ERROR", f"Este modelo está indisponível agora; tente de novo. ({erro})"), custo
        # Busca na web é permissão, não ordem: uma tentativa paga a mais, e fica a que buscou. Se falhar, fica a primeira (já paga).
        if busca and not r.get("web_search"):
            r2, erro2, c2 = _pedir(modelo, prompt, busca, pais)
            custo += c2
            if not erro2 and r2.get("web_search"):
                r = r2
        res = _formatar(modelo, r, pais)
        tmp = arq.with_name(f".{arq.name}.tmp")
        tmp.write_text(json.dumps({"salvoEm": _iso(_agora()), "resultado": res}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, arq)
        return _com_marca(res, marca), custo
    except Exception as e:  # noqa: BLE001
        motivo = f"Este modelo devolveu uma resposta ilegível; tente de novo. ({type(e).__name__})"
        return _erro(modelo, "UPSTREAM_ERROR", motivo), custo


def executar(nome, args, site):
    if not site:
        raise ErroUso(f"{nome} precisa do argumento site (id do site no hub, domínio ou nome); list_projects lista os sites")
    prompt, modelos, marca, busca, pais = _validar(args)
    with ThreadPoolExecutor(len(modelos)) as ex:  # um modelo que falha não derruba os outros
        feitos = list(ex.map(dataforseo.na_thread(lambda m: _um(site, m, prompt, busca, pais, marca)), modelos))
    return {"ok": True, "prompt": prompt, "highlightBrand": marca, "fetchedAt": _iso(_agora()), "results": [r for r, _ in feitos],
            "custo": round(sum(c for _, c in feitos), 6)}
