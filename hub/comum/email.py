"""Envio de e-mail: um cliente só, que nunca lança e devolve `{ok, id, status, erro, esperarS}`.

O provedor é a constante PROVEDOR (hoje Resend; a troca aqui, numa linha). Vale para Python e, pelo CLI, para os.sh:
`python3 -m hub.comum.email --de "Nome <user2@example.com>" --para user3@example.com --assunto "..." --texto "..."` (sai 0 se ok, 1 se não;
imprime o JSON do resultado, sem a chave).

Sempre manda User-Agent (sem ele o Cloudflare na frente da API devolve 403 em urllib), timeout e `Idempotency-Key`
(a do chamador, ou uma nova por chamada). Não tenta de novo sozinho: 429 volta com `esperarS` (do `Retry-After`) e quem
chamou reprograma.
"""
import argparse
import email.utils
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

from hub.comum.env import segredo

PROVEDOR = {"url": "https://api.resend.com/emails", "chave": "RESEND_API_KEY"}
USER_AGENT = "nimbus-hub/1.0"
ARQUIVOS_CHAVE = ("/etc/vigia-disco.env",)
TIMEOUT = 30
CORPO_MAX = 300


def _espera(valor):
    """`Retry-After`: segundos ou data HTTP -> segundos (>= 0), ou None."""
    if not valor:
        return None
    try:
        return max(0, int(float(valor)))
    except ValueError:
        pass
    try:
        return max(0, int(email.utils.parsedate_to_datetime(valor).timestamp() - time.time()))
    except (TypeError, ValueError):
        return None


def _urllib(url, cabecalhos, corpo, timeout):
    req = urllib.request.Request(url, data=corpo, headers=cabecalhos, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def enviar(de, para, assunto, html=None, texto=None, responder_para=None, anexos=None, tags=None, cabecalhos=None,
           idempotencia=None, chave=None, timeout=TIMEOUT, transporte=None):
    """`para`: texto ou lista. `transporte(url, cabecalhos, corpo_bytes, timeout) -> (status, cabecalhos, corpo_bytes)` é
    para teste. Nunca lança."""
    try:
        chave = chave or segredo(PROVEDOR["chave"], ARQUIVOS_CHAVE)
        if not chave:
            return {"ok": False, "id": None, "status": None, "erro": f"sem {PROVEDOR['chave']}", "esperarS": None}
        corpo = {"from": de, "to": [para] if isinstance(para, str) else list(para), "subject": assunto}
        for campo, valor in (("html", html), ("text", texto), ("reply_to", responder_para), ("attachments", anexos),
                             ("tags", tags), ("headers", cabecalhos)):
            if valor:
                corpo[campo] = valor
        cab = {"Authorization": f"Bearer {chave}", "Content-Type": "application/json", "User-Agent": USER_AGENT,
               "Idempotency-Key": idempotencia or str(uuid.uuid4())}
        status, resp, bruto = (transporte or _urllib)(PROVEDOR["url"], cab, json.dumps(corpo).encode("utf-8"), timeout)
        texto_resp = (bruto or b"").decode("utf-8", "replace")
        if 200 <= status < 300:
            try:
                id_ = json.loads(texto_resp).get("id")
            except (ValueError, AttributeError):
                id_ = None
            return {"ok": True, "id": id_, "status": status, "erro": None, "esperarS": None}
        minusc = {str(k).lower(): v for k, v in (resp or {}).items()}
        return {"ok": False, "id": None, "status": status, "erro": f"{status}: {texto_resp[:CORPO_MAX]}".replace(chave, "***"),
                "esperarS": _espera(minusc.get("retry-after")) if status == 429 else None}
    except TimeoutError:
        return {"ok": False, "id": None, "status": None, "erro": "timeout", "esperarS": None}
    except Exception as e:  # nunca lança: rede, DNS, SSL, corpo inválido
        msg = f"{type(e).__name__}: {e}"[:CORPO_MAX]
        return {"ok": False, "id": None, "status": None, "erro": msg.replace(chave, "***") if chave else msg, "esperarS": None}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m hub.comum.email", description=__doc__.splitlines()[0])
    ap.add_argument("--de", required=True)
    ap.add_argument("--para", required=True, action="append")
    ap.add_argument("--assunto", required=True)
    ap.add_argument("--texto")
    ap.add_argument("--html")
    ap.add_argument("--idempotencia")
    a = ap.parse_args(argv)
    if not (a.texto or a.html):
        ap.error("precisa de --texto ou --html")
    r = enviar(a.de, a.para, a.assunto, html=a.html, texto=a.texto, idempotencia=a.idempotencia)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
