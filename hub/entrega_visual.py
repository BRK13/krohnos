"""Entrega visual só com evidência de que alguém olhou (lição 5 `entrega_visual_sem_conferir`).

Tela, render, vídeo, áudio ou peça 3D entregues sem olhar/ouvir contra a medida, a foto ou o pedido: o dono via o erro
(mapa ilegível, hero, mesa em L, voz). O recibo de pronto (`hub/recibo.py`) agora exige,
para issue visual concluída depois de ENTRA_EM, o arquivo `$HUB_DATA/claude/olhei/<ISSUE>.olhei.json`:

    python3 -m hub.entrega_visual gerar prints/*.png [--medidas medidas.json] # mede e monta o checklist do pedido
    (editar o arquivo: cada item do checklist ganha `olhei: true` e uma `observacao` do que se viu/ouviu)
    python3 -m hub.entrega_visual conferir # o que o recibo vai conferir

O que `gerar` mede de verdade (sem modelo julgando): imagem (largura, altura, em branco, hash), vídeo e áudio (ffprobe: duração,
trilhas; loudnorm: LUFS) e, no `medidas.json` ({"nome": {"esperado": 100, "obtido": 101}}), a tolerância pedida ("±2%").
O que o pedido exige vira requisito objetivo: larguras de tela ("desktop e celular", "1440 e 390"), "antes e depois", "-14 LUFS",
"±N%". Sem a evidência, o recibo reabre a issue com o motivo. Ferramentas: `inspect.mjs` (design-taste) e `earshot` geram
os prints e as medidas; o verificador só exige que existam e batam com o pedido."""
import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from hub import conta, data

ENTRA_EM = dt.datetime(2026, 10, 18, tzinfo=dt.timezone.utc)  # só issue concluída depois disto: nada do passado reabre
LARGURA_MIN = 300
OBS_MIN = 15
IMG, VID, AUD = (".png", ".jpg", ".jpeg", ".webp", ".gif"), (".mp4", ".mov", ".webm"), (".mp3", ".wav", ".m4a", ".ogg")
RE_VISUAL = re.compile(r"\bprints?\b|screenshots?|antes e depois|antes/depois|\bdesktop\b|\bcelular\b|\b1440\b|\bmp4\b|\bmp3\b|"
                       r"v[íi]deo|[áa]udio|render|\bplantas?\b|\bfolhas?\b|\b3d\b|protótipo", re.I)
RE_LUFS = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*LUFS", re.I)
RE_TOL = re.compile(r"[±+/-]{1,3}\s*(\d+(?:[.,]\d+)?)\s*%")
RE_LARGURAS = re.compile(r"\b(\d{3,4})\s*(?:e|,|/|x)\s*(\d{3,4})\b")
RE_TELAS = re.compile(r"desktop e (?:celular|mobile)|celular e desktop|mobile e desktop", re.I)
RE_ANTES_DEPOIS = re.compile(r"antes e depois|antes/depois|antes → depois|antes → depois", re.I)


def arquivo(ident, base_dados=None):
    return conta.pasta(base_dados) / "olhei" / f"{ident.upper()}.olhei.json"


def exige_olhei(titulo, pronto, concluida=None):
    """Issue visual (o pedido fala em print, vídeo, áudio, render, planta, antes e depois...) concluída depois de ENTRA_EM."""
    if concluida and conta.de_iso(concluida) < ENTRA_EM:
        return False
    return bool(RE_VISUAL.search(f"{titulo or ''}\n{pronto or ''}"))


def requisitos(titulo, pronto):
    """Requisitos objetivos tirados do pedido. Cada um: {"id", "texto", "tipo", ...}; o resto vira item de checklist."""
    texto = f"{titulo or ''}\n{pronto or ''}"
    reqs = []
    if RE_TELAS.search(texto):
        reqs.append({"id": "telas", "tipo": "larguras", "larguras": [1440, 390], "texto": "prints em desktop (1440) e celular (390)"})
    elif m := RE_LARGURAS.search(texto):
        a, b = sorted(int(x) for x in m.groups())[::-1]
        if a >= 1000 and 300 <= b <= 500:
            reqs.append({"id": "telas", "tipo": "larguras", "larguras": [a, b], "texto": f"prints em {a} e {b}"})
    if RE_ANTES_DEPOIS.search(texto):
        reqs.append({"id": "antes_depois", "tipo": "antes_depois", "texto": "o antes e o depois anexados"})
    if m := RE_LUFS.search(texto):
        reqs.append({"id": "lufs", "tipo": "lufs", "alvo": float(m.group(1).replace(",", ".")), "tolerancia": 1.5,
                     "texto": f"áudio em {m.group(1)} LUFS (±1,5)"})
    if m := RE_TOL.search(texto):
        reqs.append({"id": "tolerancia", "tipo": "tolerancia", "pct": float(m.group(1).replace(",", ".")),
                     "texto": f"medida dentro de ±{m.group(1)}% do real (medidas.json)"})
    return reqs


def _linhas_do_pedido(pronto):
    """Cada marcador (`*`, `-`, número) do "Pronto quando" vira um item do checklist."""
    itens = [re.sub(r"^\s*(?:[*\-•]|\d+[.)])\s*", "", l).strip(" *`") for l in (pronto or "").splitlines()
             if re.match(r"^\s*(?:[*\-•]|\d+[.)])\s+\S", l)]
    return [i for i in itens if len(i) > 3] or ([" ".join((pronto or "").split())[:300]] if (pronto or "").strip() else [])


def _run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return f"{p.stdout}\n{p.stderr}"  # o loudnorm imprime no stderr
    except (OSError, subprocess.SubprocessError):
        return ""


def medir(caminho):
    """Medidas objetivas de um artefato. Nunca levanta: arquivo ilegível vira {"erro": ...}."""
    p = Path(caminho)
    m = {"arquivo": p.name, "caminho": str(p.resolve()), "tamanho": 0, "sha256": ""}
    try:
        corpo = p.read_bytes()
    except OSError as e:
        return {**m, "erro": f"não abriu ({type(e).__name__})"}
    m.update(tamanho=len(corpo), sha256=hashlib.sha256(corpo).hexdigest()[:16])
    ext = p.suffix.lower()
    if ext in IMG:
        try:
            from PIL import Image, ImageStat
            with Image.open(p) as im:
                g = im.convert("L")
                m.update(tipo="imagem", largura=im.width, altura=im.height, desvio=round(ImageStat.Stat(g).stddev[0], 2))
        except Exception as e:  # noqa: BLE001
            return {**m, "erro": f"imagem inválida ({type(e).__name__})"}
    elif ext in VID or ext in AUD:
        try:
            j = json.loads(_run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(p)]) or "{}")
        except ValueError:
            j = {}
        tipos = {s.get("codec_type") for s in j.get("streams", [])}
        m.update(tipo="video" if "video" in tipos else "audio", duracao=float((j.get("format") or {}).get("duration") or 0),
                 tem_audio="audio" in tipos)
        if "audio" in tipos:
            saida = _run(["ffmpeg", "-nostats", "-i", str(p), "-af", "loudnorm=print_format=json", "-vn", "-f", "null", "-"])
            if mm := re.search(r'"input_i"\s*:\s*"(-?[\d.]+|-inf)"', saida):
                m["lufs"] = float(mm.group(1)) if mm.group(1) != "-inf" else -99.0
    else:
        m["tipo"] = "outro"
    return m


def problema_objetivo(m):
    """Motivo pelo qual a medida já reprova o artefato, ou ''."""
    if m.get("erro"):
        return m["erro"]
    if not m.get("tamanho"):
        return "arquivo vazio"
    if m.get("tipo") == "imagem":
        if m["largura"] < LARGURA_MIN:
            return f"largura {m['largura']} px (mínimo {LARGURA_MIN})"
        if m.get("desvio", 99) < 3:
            return "imagem em branco (quase uma cor só)"
    if m.get("tipo") in ("video", "audio"):
        if m.get("duracao", 0) <= 0:
            return "sem duração"
        if m.get("tem_audio") and m.get("lufs", 0) <= -60:
            return "áudio mudo"
    return ""


def montar(ident, titulo, pronto, arquivos, medidas=None, agora=None):
    arts = [medir(a) for a in arquivos]
    return {"ident": ident.upper(), "gerado_em": conta.iso(agora or conta.agora()), "artefatos": arts,
            "medidas": medidas or {}, "requisitos": requisitos(titulo, pronto),
            "checklist": [{"item": i, "olhei": False, "observacao": ""} for i in _linhas_do_pedido(pronto)]}


def _norm(t):
    return " ".join(str(t or "").lower().split())


def _mudou(m):
    """O arquivo medido ainda existe e tem o mesmo conteúdo? Senão a evidência é de outra entrega."""
    if not m.get("caminho"):
        return "evidência sem o caminho do arquivo: rode `gerar` de novo"
    try:
        corpo = Path(m["caminho"]).read_bytes()
    except OSError:
        return "o arquivo sumiu depois de `gerar`: rode `gerar` de novo"
    if hashlib.sha256(corpo).hexdigest()[:16] != m.get("sha256"):
        return "o arquivo mudou depois de `gerar` (hash diferente): rode `gerar` de novo"
    return ""


def _lado(m):
    n = str(m.get("arquivo", "")).lower()
    return "antes" if "antes" in n and "depois" not in n else "depois" if "depois" in n and "antes" not in n else ""


def faltas(ev, titulo, pronto):
    """O que falta na evidência para o recibo liberar (lista vazia = ok). `ev` é o conteúdo do `.olhei.json` (ou None)."""
    if not ev:
        return ["falta a evidência de que a entrega foi olhada: rode `python3 -m hub.entrega_visual gerar <ISSUE> <arquivos>`, "
                "confira cada item do checklist e marque `olhei` com a `observacao` do que viu"]
    out, arts = [], []
    if not ev.get("artefatos"):
        out.append("a evidência não lista nenhum artefato (print, vídeo ou áudio)")
    for m in ev.get("artefatos") or []:
        if motivo := _mudou(m):
            out.append(f"{m.get('arquivo', '?')}: {motivo}")
            arts.append(m)
            continue
        m = {**medir(m["caminho"]), "arquivo": m.get("arquivo")}  # medida do arquivo de agora, não a do JSON editável
        if motivo := problema_objetivo(m):
            out.append(f"{m.get('arquivo', '?')}: {motivo}")
        arts.append(m)
    for r in requisitos(titulo, pronto):  # requisito do pedido atual, não do que o agente escreveu
        if r["tipo"] == "larguras":
            imgs = [m for m in arts if m.get("tipo") == "imagem"]
            par = any(q["tipo"] == "antes_depois" for q in requisitos(titulo, pronto))
            # pedido com antes e depois: cada lado precisa das duas larguras, não só uma combinação
            grupos = [("antes", [m for m in imgs if _lado(m) == "antes"]), ("depois", [m for m in imgs if _lado(m) == "depois"])] if par else [("", imgs)]
            for lado, g in grupos:
                if par and not g:
                    continue  # lado sem arquivo já cai em "falta o par antes e depois"
                tem = {int(m.get("largura") or 0) for m in g}
                for w in r["larguras"]:
                    if not any(abs(t - w) <= max(20, w * 0.05) for t in tem):
                        out.append(f"falta print {lado + ' ' if lado else ''}com largura ~{w} px ({r['texto']})")
        elif r["tipo"] == "antes_depois":
            nomes = [str(m.get("arquivo", "")).lower() for m in arts]  # um arquivo com os dois nomes não prova o "antes"
            if not any("antes" in n and "depois" not in n for n in nomes) or not any("depois" in n and "antes" not in n for n in nomes):
                out.append("falta o par antes e depois (nomes de arquivo com `antes` e `depois`)")
        elif r["tipo"] == "lufs":
            aud = [m for m in arts if "lufs" in m]
            if not aud:
                out.append(f"falta áudio medido ({r['texto']})")
            for m in aud:
                if abs(m["lufs"] - r["alvo"]) > r["tolerancia"]:
                    out.append(f"{m['arquivo']}: {m['lufs']:.1f} LUFS, pedido {r['alvo']:g} ±{r['tolerancia']:g}")
        elif r["tipo"] == "tolerancia":
            med = ev.get("medidas") or {}
            if not med:
                out.append(f"falta o medidas.json ({r['texto']})")
            for nome, v in med.items():
                try:
                    esp, obt = float(v["esperado"]), float(v["obtido"])
                except (KeyError, TypeError, ValueError):
                    out.append(f"{nome}: medida sem `esperado` e `obtido` numéricos")
                    continue
                if esp and abs(obt - esp) / abs(esp) * 100 > r["pct"]:
                    out.append(f"{nome}: obtido {obt:g} contra {esp:g} (limite ±{r['pct']:g}%)")
    pedido = _linhas_do_pedido(pronto)
    lista = ev.get("checklist") or []
    feitos = {_norm(c.get("item")) for c in lista}
    for i in pedido:
        if _norm(i) not in feitos:
            out.append(f"item do pedido fora do checklist: {i[:80]}")
    for c in lista:
        if not c.get("olhei") or len((c.get("observacao") or "").strip()) < OBS_MIN:
            out.append(f"item sem conferência: {str(c.get('item', ''))[:80]}")
    return out


def ler(ident, base_dados=None):
    try:
        return json.loads(arquivo(ident, base_dados).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m hub.entrega_visual")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gerar", help="mede os artefatos e monta o .olhei.json com o checklist do pedido")
    g.add_argument("ident")
    g.add_argument("arquivos", nargs="+")
    g.add_argument("--medidas", help="JSON {nome: {esperado, obtido}}")
    c = sub.add_parser("conferir", help="o que o recibo de pronto vai exigir desta issue")
    c.add_argument("ident")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    from hub import linear
    i = linear.gql("query($i: String!) { issue(id: $i) { title description } }", i=a.ident.upper())["issue"]
    from hub import recibo
    pronto = recibo.pronto_quando(i.get("description"))
    if a.cmd == "gerar":
        med = json.loads(Path(a.medidas).read_text(encoding="utf-8")) if a.medidas else None
        if med is not None and not (isinstance(med, dict) and all(
                isinstance(v, dict) and all(isinstance(v.get(k), (int, float)) for k in ("esperado", "obtido")) for v in med.values())):
            print("medidas.json inválido: use {nome: {\"esperado\": número, \"obtido\": número}}", file=sys.stderr)
            return 2
        ev = montar(a.ident, i["title"], pronto, a.arquivos, med)
        destino = arquivo(a.ident)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"gravado: {destino}\nPreencha `olhei` e `observacao` de cada item do checklist; depois: conferir {a.ident}")
        return 0
    f = faltas(ler(a.ident), i["title"], pronto)
    print("\n".join(f) if f else "ok: a evidência cobre o pedido")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
