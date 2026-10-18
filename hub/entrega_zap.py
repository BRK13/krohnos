"""Toda entrega visual vai sozinha para o WhatsApp do dono. Regra dele (16/10): "tudo que é entrega que eu
preciso ver tem que ir pro zap". Antes cada issue dependia de o agente lembrar (reels da, PDFs da e o
.docx da ficaram só no Linear). Agora, a cada 5 min (tick da agenda, junto do recibo de pronto), toda
issue em Done passa por aqui (só a versão final; In Review é iteração e não vai; issue reaberta depois de Done
não manda "pronta" de novo):
- entregáveis visíveis = anexos da issue (registro do `anexar`, anexos do Linear) + arquivos e links citados no último
  comentário do agente: PDF, DOCX, imagem, vídeo, áudio, link de página nova ou de prévia;
- sem duplicar: guarda o hash de cada arquivo por issue (recibos de `entregas-zap.json`) e também conta o que o próprio
  agente já mandou pela caixa de avisos (arquivos do aviso que cita a issue);
- manda pela caixa de avisos COM `para` (o admin do bot): 1 linha "KRN-x pronta: <título curto>" e os arquivos direto
  (PDF, imagem, vídeo), no máximo 4; com mais, as imagens viram um mosaico (uma imagem). Zip nunca. O porteiro (hub/comum/porteiro.py) ainda segura madrugada, teto
  e entrega repetida;
- limites do WhatsApp: vídeo acima de 64 MB e outro arquivo acima de 1 GB não vão: só o link do Linear;
- fica fora: issue só de código, infra ou SEO (sem arquivo visível nem link de prévia): entra no resumo diário.
`python3 -m hub.entrega_zap reenviar --horas 48 [--simular]` refaz a varredura da janela; `issue KRN-x` uma só."""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from hub import conta, config, data, linear

VISIVEL = {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov", ".webm",
           ".mp3", ".m4a", ".ogg", ".wav"}
PRIORIDADE = {".pdf": 0, ".docx": 0, ".mp4": 1, ".mov": 1, ".webm": 1, ".png": 2, ".jpg": 2, ".jpeg": 2, ".gif": 2,
              ".webp": 2, ".mp3": 3, ".m4a": 3, ".ogg": 3, ".wav": 3}
LIMITE_VIDEO = 64 * 1024 * 1024  # vídeo acima disso não vai (zip nunca): fica no Linear
LIMITE_ARQUIVO = 1024 * 1024 * 1024  # outro arquivo acima disso: só o link do Linear
IMAGENS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
VIDEOS = {".mp4", ".mov", ".webm"}
MAX_ARQUIVOS = 4  # por mensagem, direto; passou disso, imagens viram um mosaico (zip nunca)
MAX_MOSAICO = 12  # imagens na grade do mosaico
INTERVALO = dt.timedelta(minutes=5)
RE_MD = re.compile(r"!?\[([^\]]*)\]\((https?://[^)\s]+)\)")
RE_URL = re.compile(r"https?://[^\s)>\]]+")
RE_CAMINHO = re.compile(r"(?<![\w/.])(/(?:home|var|tmp|opt|srv)/[^\s`'\"()<>\[\]]+)")
RE_PREVIA = re.compile(r"vercel\.app|netlify\.app|pages\.dev|sslip\.io|traefik\.me|/previa|/preview|[-.]previa[-.]|"
                       r"[-.]preview[-.]", re.I)
RE_PAGINA_NOVA = re.compile(r"p[áa]gina nova|nova p[áa]gina|pr[ée]via|preview|rascunho|publicad", re.I)
IGNORAR_HOST = ("github.com", "linear.app", "localhost", "127.0.0.1")
Q_ISSUES = ("query($d: DateTimeOrDuration!, $after: String) { issues(first: 100, after: $after, filter: {completedAt: {gt: $d}, state: {name: {in: "
            "[\"Done\"]}}}) { nodes { id identifier title updatedAt state { name } "
            "history(first: 50) { nodes { fromState { type } toState { type } } pageInfo { hasNextPage endCursor } } "
            "attachments { nodes { url title } } comments(last: 30) { nodes { createdAt body user { app } } } } "
            "pageInfo { hasNextPage endCursor } } }")
MAX_PAGINAS = 20  # trava de segurança contra cursor que não avança



def _ext(nome):
    return Path(urllib.parse.unquote(urllib.parse.urlparse(nome).path) if "://" in nome else nome).suffix.lower()


def sha_arquivo(caminho):
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def pasta_dados_bot(caixa=None):
    """DATA_DIR do bot: os caminhos dos avisos são relativos a ele (a caixa fica em <DATA_DIR>/avisos)."""
    return Path(caixa or conta.CAIXA_AVISOS).parent


def admin_numero():
    """Número do admin do bot (quem recebe com `para`): o mesmo que o porteiro tem por dono (config dono.whatsapp,
    TRIAGEM_ADMIN ou o env do contêiner). None: o aviso vai sem `para` (o bot manda ao dono do mesmo jeito)."""
    from hub.comum import porteiro
    n = porteiro.numero_dono()
    return n if re.fullmatch(r"\d{10,15}", n or "") else None


# ---- coleta ----

def _baixar(url, destino):
    """Baixa um anexo do Linear (precisa da chave) para `destino`; devolve o caminho."""
    req = urllib.request.Request(url, headers={"Authorization": linear.chave(), "User-Agent": "kronos-entrega-zap"})
    destino.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=120) as r, open(destino, "wb") as f:  # noqa: S310 — URL do próprio Linear
        shutil.copyfileobj(r, f)
    return destino


def _eh_linear(url):
    return urllib.parse.urlparse(url).hostname == "uploads.linear.app"


def _hosts_dos_sites():
    try:
        from hub import sites
        return {s["dominio"].lower() for s in sites.ativos() if s.get("dominio")}
    except Exception:  # noqa: BLE001
        return set()


def _arquivo_remoto(url):
    """URL direta de PDF, imagem, vídeo etc. fora do Linear: vai como link no aviso (não baixa, não leva a chave)."""
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    return (u.scheme == "https" and bool(host) and not _eh_linear(url) and _ext(url) in VISIVEL
            and not any(host == h or host.endswith("." + h) for h in IGNORAR_HOST))


def _link_visivel(url, comentario, hosts):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not host or any(host == h or host.endswith("." + h) for h in IGNORAR_HOST) or _ext(url) in VISIVEL | {".zip"}:
        return False
    if RE_PREVIA.search(url):
        return True
    path = urllib.parse.urlparse(url).path.strip("/")
    return bool(path) and any(host == h or host.endswith("." + h) for h in hosts) and bool(RE_PAGINA_NOVA.search(comentario))


def candidatos(issue, base_dados=None):
    """Entregáveis visíveis da issue, sem baixar nada: [{"tipo": "arquivo"|"linear"|"link", "nome", "origem"}]."""
    ident, vistos, saida = issue["identifier"], set(), []

    def somar(tipo, nome, origem):
        if origem not in vistos:
            vistos.add(origem)
            saida.append({"tipo": tipo, "nome": nome, "origem": origem})
    with contextlib.suppress(OSError):  # registro do `anexar`: arquivo local (quando gravado) ou link do Linear
        for l in (Path(base_dados or data()) / "claude" / "anexos" / f"{ident.upper()}.jsonl").read_text().splitlines():
            with contextlib.suppress(ValueError):
                r = json.loads(l)
                if (r.get("arquivo") and Path(r["arquivo"]).is_file() and _ext(r["arquivo"]) in VISIVEL
                        and (not r.get("sha") or sha_arquivo(r["arquivo"]) == r["sha"])):  # sobrescrito depois do anexar: vale o publicado
                    somar("arquivo", Path(r["arquivo"]).name, r["arquivo"])
                    continue
                for nome, url in RE_MD.findall(r.get("link") or ""):
                    if _eh_linear(url) and _ext(nome) in VISIVEL:
                        somar("linear", nome, url)
    for a in (issue.get("attachments") or {}).get("nodes") or []:
        url = a.get("url") or ""
        if _eh_linear(url) and _ext(a.get("title") or "") in VISIVEL:
            somar("linear", a["title"], url)
    comentarios = [c for c in (issue.get("comments") or {}).get("nodes") or [] if (c.get("user") or {}).get("app")]
    ultimo = sorted(comentarios, key=lambda c: c.get("createdAt") or "")[-1:] or [{}]
    texto = ultimo[0].get("body") or ""
    hosts = _hosts_dos_sites()
    for nome, url in RE_MD.findall(texto):
        if _eh_linear(url) and _ext(nome) in VISIVEL:
            somar("linear", nome, url)
    for caminho in RE_CAMINHO.findall(texto):
        caminho = caminho.rstrip(".,;:")
        if _ext(caminho) in VISIVEL and Path(caminho).is_file():
            somar("arquivo", Path(caminho).name, caminho)
    for url in RE_URL.findall(texto):
        url = url.rstrip(".,;:")
        if _link_visivel(url, texto, hosts) or _arquivo_remoto(url):
            somar("link", url, url)
    return saida


# ---- estado e dedup ----

def _estado_arq(base_dados=None):
    return conta.pasta(base_dados) / "entregas-zap.json"


def _ler(base_dados=None):
    with contextlib.suppress(OSError, ValueError):
        return json.loads(_estado_arq(base_dados).read_text(encoding="utf-8"))
    return {}


def _gravar(est, base_dados=None):
    arq = _estado_arq(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps(est, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, arq)


def ja_mandados_pelo_agente(ident, caixa=None, desde=None):
    """Hashes dos arquivos que algum aviso (na fila ou enviado) da issue já levou: o agente já mandou, o hub não repete."""
    caixa, saida = Path(caixa or conta.CAIXA_AVISOS), set()
    base, nome = pasta_dados_bot(caixa), f"linear-{ident.lower()}"
    for arq in [*caixa.glob("*.json"), *(caixa / "enviados").glob("*.json")]:
        with contextlib.suppress(OSError, ValueError):
            if desde and arq.stat().st_mtime < desde:
                continue
            a = json.loads(arq.read_text(encoding="utf-8"))
            if not a.get("arquivos") or not (a.get("tarefa") == nome or re.search(rf"\b{re.escape(ident)}\b", a.get("texto") or "", re.I)):
                continue
            for rel in a["arquivos"]:
                with contextlib.suppress(OSError):
                    saida.add(sha_arquivo(base / rel))
    return saida


def _materializar(c, issue_est, pasta_tmp):
    """Arquivo local + hash do candidato; link devolve o hash da URL. O download do Linear é lembrado por URL."""
    if c["tipo"] == "link":
        return None, "url:" + hashlib.sha256(c["origem"].encode()).hexdigest()
    if c["tipo"] == "arquivo":
        return Path(c["origem"]), sha_arquivo(c["origem"])
    destino = pasta_tmp / hashlib.sha256(c["origem"].encode()).hexdigest()[:8] / re.sub(r"[^\w.\-]+", "_", c["nome"])
    _baixar(c["origem"], destino)
    sha = sha_arquivo(destino)
    issue_est.setdefault("urls", {})[c["origem"]] = sha
    return destino, sha


# ---- montagem do aviso ----

def titulo_curto(titulo, limite=60):
    t = re.sub(r"\s+", " ", titulo or "").strip()
    return t if len(t) <= limite else t[:limite - 1].rstrip() + "…"


def _mosaico(imagens, destino, lado=480, colunas=3):
    """Uma imagem só com as prévias de todas (grade). None se o Pillow não está ou alguma imagem não abre."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        n = len(imagens)
        cols = min(colunas, n)
        linhas = -(-n // cols)
        folha = Image.new("RGB", (cols * lado, linhas * lado), "white")
        for i, caminho in enumerate(imagens):
            with Image.open(caminho) as im:
                im = im.convert("RGB")
                im.thumbnail((lado, lado))
                folha.paste(im, ((i % cols) * lado + (lado - im.width) // 2, (i // cols) * lado + (lado - im.height) // 2))
        destino.parent.mkdir(parents=True, exist_ok=True)
        folha.save(destino, "JPEG", quality=85)
        return destino
    except Exception:  # noqa: BLE001 — imagem estragada ou grande demais: vão os arquivos soltos
        return None


def montar(ident, titulo, arquivos, links, pasta_envio):
    """(texto, [Path a mandar]). UMA mensagem por entrega, uma linha de texto (o que é) e os arquivos DIRETO (PDF,
    imagem, vídeo), no máximo MAX_ARQUIVOS. Passou disso, as imagens viram um mosaico (uma imagem só). Zip nunca: o
    dono não abre no celular. O que não cabe fica anexado na issue (o texto avisa)."""
    texto = [f"{ident} pronta: {titulo_curto(titulo)}"]
    ordem = sorted(arquivos, key=lambda a: (PRIORIDADE.get(a.suffix.lower(), 9), -a.stat().st_size, a.name))
    fora = [a for a in ordem if a.stat().st_size > (LIMITE_VIDEO if a.suffix.lower() in VIDEOS else LIMITE_ARQUIVO)]
    ordem = [a for a in ordem if a not in fora]
    imagens = [a for a in ordem if a.suffix.lower() in IMAGENS]
    if len(ordem) > MAX_ARQUIVOS and len(imagens) > 1:
        mosaico = _mosaico(imagens[:MAX_MOSAICO], pasta_envio / f"{ident}-previas.jpg")
        if mosaico:
            fora += imagens[MAX_MOSAICO:]
            ordem = [a for a in ordem if a not in imagens] + [mosaico]
    if len(ordem) > MAX_ARQUIVOS:
        fora += ordem[MAX_ARQUIVOS:]
        ordem = ordem[:MAX_ARQUIVOS]
    if fora:
        texto.append("Não coube no WhatsApp, veja na issue: " + ", ".join(a.name for a in fora)
                     + f" https://linear.app/fulanopereira/issue/{ident}")
    texto += links
    return "\n".join(texto), ordem


def _copiar_para_bot(arquivos, ident, base_bot):
    """Copia para <DATA_DIR>/envios/entregas/<ID>/ e devolve os caminhos relativos que o aviso leva."""
    destino, rels = base_bot / "envios" / "entregas" / ident.upper(), []
    destino.mkdir(parents=True, exist_ok=True)
    for a in arquivos:
        alvo, sha = destino / a.name, sha_arquivo(a)
        if alvo.exists() and sha_arquivo(alvo) != sha:  # mesmo nome, conteúdo diferente: não sobrescreve o que a outra mensagem leva
            alvo = destino / f"{a.stem}-{sha[:8]}{a.suffix}"
        if not alvo.exists():
            shutil.copyfile(a, alvo)
        rels.append(str(alvo.relative_to(base_bot)))
    return rels


# ---- varredura ----

Q_HISTORICO = ("query($i: String!, $a: String) { issue(id: $i) { history(first: 50, after: $a) { nodes { fromState { type } "
               "toState { type } } pageInfo { hasNextPage endCursor } } } }")


def _historico(issue, consulta=None):
    """Todas as mudanças de estado da issue: segue a paginação (o histórico passa de 50 em issue muito mexida)."""
    h = issue.get("history") or {}
    nos, info = list(h.get("nodes") or []), h.get("pageInfo") or {}
    for _ in range(MAX_PAGINAS):
        if not info.get("hasNextPage") or not info.get("endCursor"):
            break
        h = ((consulta or linear.gql)(Q_HISTORICO, i=issue["id"], a=info["endCursor"])["issue"] or {}).get("history") or {}
        nos += h.get("nodes") or []
        info = h.get("pageInfo") or {}
    return nos


def foi_reaberta(issue, consulta=None):
    """Issue que já foi Done e saiu de Done (auditoria, orquestrador): a conclusão de agora não é entrega nova."""
    for h in _historico(issue, consulta):
        if (h.get("fromState") or {}).get("type") == "completed" and (h.get("toState") or {}).get("type") != "completed":
            return True
    return False


def entregar(issue, base_dados=None, caixa=None, avisar_fn=None, simular=False, agora=None):
    """Manda o que ainda não foi ao dono. Devolve {"enviados": [nomes], "ja": n, "texto": str|None}."""
    ident, agora = issue["identifier"], agora or conta.agora()
    if ((issue.get("state") or {}).get("name") or "Done") != "Done":  # só a versão final; revisão não vai
        return {"enviados": [], "ja": 0, "texto": None}
    from hub import portao_done
    # reaberta pela auditoria ou que já saiu de Done antes (e): termina em silêncio
    if portao_done.silenciada(ident, base_dados) or foi_reaberta(issue):
        return {"enviados": [], "ja": 0, "texto": None}
    cands = candidatos(issue, base_dados)
    if not cands:
        return {"enviados": [], "ja": 0, "texto": None}
    est = _ler(base_dados)
    ie = est.setdefault(ident, {"sha": {}, "urls": {}})
    # Uma mensagem por issue (dono, 16/10: chegou de novo com prévias novas, "loop infernal"): issue que já foi
    # ao zap não vai de novo, nem com arquivo novo; o que entrar depois fica no Linear e no resumo diário.
    if ie["sha"] or ja_mandados_pelo_agente(ident, caixa):
        return {"enviados": [], "ja": len(cands), "texto": None}
    ja = set()
    tmp = conta.pasta(base_dados) / "entregas-zap-tmp" / ident
    shutil.rmtree(tmp, ignore_errors=True)
    novos, links, n_ja = [], [], 0
    try:
        for c in cands:
            sha_conhecido = ie["urls"].get(c["origem"])
            if sha_conhecido and sha_conhecido in ja:  # já mandado: nem baixa de novo
                n_ja += 1
                continue
            caminho, sha = _materializar(c, ie, tmp)
            if sha in ja or sha in {n[0] for n in novos}:  # o mesmo arquivo anexado duas vezes vai uma só
                n_ja += 1
            elif c["tipo"] == "link":
                links.append(c["origem"])
                novos.append((sha, c["origem"]))
            else:
                novos.append((sha, caminho))
        if not novos:
            if not simular:
                _gravar(est, base_dados)
            return {"enviados": [], "ja": n_ja, "texto": None}
        arquivos = [c for _, c in novos if isinstance(c, Path)]
        base_bot = pasta_dados_bot(caixa)
        pasta_envio = tmp / "envio"
        texto, a_mandar = montar(ident, issue.get("title"), arquivos, links, pasta_envio)
        if simular:
            return {"enviados": [a.name for a in a_mandar] + links, "ja": n_ja, "texto": texto}
        rels = _copiar_para_bot(a_mandar, ident, base_bot)
        (avisar_fn or conta.avisar)(texto, agora_=True, caixa=caixa, arquivos=rels, para=admin_numero(), origem="entrega_zap")
        for sha, _ in novos:
            ie["sha"][sha] = conta.iso(agora)
        _gravar(est, base_dados)
        return {"enviados": [a.name for a in a_mandar] + links, "ja": n_ja, "texto": texto}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _issues_da_janela(consulta, desde):
    """Todas as issues da janela, seguindo a paginação (a consulta devolve 100 por página)."""
    depois, vistas = None, []
    for _ in range(MAX_PAGINAS):
        r = consulta(Q_ISSUES, d=desde, after=depois)["issues"]
        vistas += r["nodes"]
        info = r.get("pageInfo") or {}
        if not info.get("hasNextPage") or not info.get("endCursor"):
            break
        depois = info["endCursor"]
    return vistas


def vigiar(consulta=None, base_dados=None, agora=None, log=print, horas=6, forcar=False, avisar_fn=None, caixa=None,
           simular=False):
    """Varre as issues CONCLUÍDAS nas últimas `horas` (a cada INTERVALO, salvo `forcar`). Pela data de conclusão, não
    de atualização: issue antiga mexida hoje (comentário, etiqueta) não vai ao zap."""
    consulta, agora = consulta or linear.gql, agora or conta.agora()
    est = _ler(base_dados)
    if not forcar and est.get("_rodou") and agora - conta.de_iso(est["_rodou"]) < INTERVALO:
        return []
    est["_rodou"] = conta.iso(agora)
    _gravar(est, base_dados)
    feitos = []
    for i in _issues_da_janela(consulta, conta.iso(agora - dt.timedelta(hours=horas))):
        try:
            r = entregar(i, base_dados, caixa, avisar_fn, simular, agora)
        except Exception as e:  # noqa: BLE001 — uma issue ruim não trava as outras; tenta de novo no próximo tick
            log(f"{i['identifier']}: entrega ao WhatsApp falhou ({type(e).__name__}: {str(e)[:150]})")
            continue
        if r["enviados"]:
            log(f"{i['identifier']}: {'simulado' if simular else 'enviado ao WhatsApp'}: {', '.join(r['enviados'])}")
            feitos.append({"issue": i["identifier"], **r})
    return feitos


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hub.entrega_zap", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reenviar", help="varre as issues Done da janela e manda o que não chegou")
    r.add_argument("--horas", type=float, default=48)
    r.add_argument("--simular", action="store_true", help="só mostra o que mandaria")
    u = sub.add_parser("issue", help="uma issue só")
    u.add_argument("ident")
    u.add_argument("--simular", action="store_true")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    if a.cmd == "reenviar":
        feitos = vigiar(horas=a.horas, forcar=True, simular=a.simular)
    else:
        i = linear.gql("query($i: String!) { issue(id: $i) { id identifier title updatedAt state { name } attachments "
                       "{ nodes { url title } } comments(last: 30) { nodes { createdAt body user { app } } } "
                       "history(first: 50) { nodes { fromState { type } toState { type } } pageInfo { hasNextPage endCursor } } } }",
                       i=a.ident)["issue"]
        feitos = [{"issue": i["identifier"], **entregar(i, simular=a.simular)}]
    print(json.dumps(feitos, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
