"""Envio de e-mail: um cliente só, que nunca lança e devolve `{ok, id, status, erro, esperarS}`.

O provedor sai da chave `EMAIL_PROVEDOR` (`ses` ou `resend`, ambiente ou /etc/vigia-disco.env); sem ela vale PROVEDOR_PADRAO.
Valor inválido não cai calado em outro provedor: volta `ok False`. SES (sesv2, us-east-2) é o padrão; Resend é a reserva
. A assinatura de `enviar` e o retorno não mudam com o provedor. Vale para Python e, pelo CLI, para os.sh:
`python3 -m hub.comum.email --de "Nome <user1@example.com>" --para user2@example.com --assunto "..." --texto "..."` (sai 0 se ok, 1 se não;
imprime o JSON do resultado, sem a chave).

Sempre manda User-Agent (sem ele o Cloudflare na frente da API devolve 403 em urllib), timeout e `Idempotency-Key`
(a do chamador, ou uma nova por chamada). Não tenta de novo sozinho: 429 volta com `esperarS` (do `Retry-After`) e quem
chamou reprograma. O SES não tem idempotência: `idempotencia` é ignorada e um reenvio manda de novo. `boto3` só é importado
no caminho SES.
"""
import argparse
import base64
import email.message
import email.policy
import email.utils
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

from pathlib import Path

from hub import home_agentes
from hub.comum.env import ler_env, segredo

# SES é o padrão desde a (F0 entregou domínio e chaves por app; prova dos fluxos em 20/10). Resend é a reserva:
# `EMAIL_PROVEDOR=resend` volta sem mexer no código. Precisa de SES_ACCESS_KEY_ID/SES_SECRET_ACCESS_KEY no ambiente ou
# em /etc/vigia-disco.env.
PROVEDOR_PADRAO = "ses"
PROVEDORES = ("ses", "resend")
SES_REGION = "us-east-2"
PROVEDOR = {"url": "https://api.resend.com/emails", "chave": "RESEND_API_KEY"}
USER_AGENT = "nimbus-hub/1.0"
ARQUIVOS_CHAVE = ("/etc/vigia-disco.env",)
# Chave por app (stack ses-chaves-por-app): cada usuário IAM /ses-app/ses-<app> só envia pelo próprio domínio.
# Domínio do remetente -> apps que o enviam, em ordem de preferência (o app dedicado antes do `hub`, que cobre os 5 sites).
# O arquivo vem de SES_APPS_DIR (só ela, se definida), senão de ~/.config/ses-apps ou de <home_agentes>/.config/ses-apps (o executor roda com outro HOME).
APPS_POR_DOMINIO = {
    "clientec-com.example": ("asite01", "hub"),
    "clientea-com.example": ("fsite06", "hub"),
    "clientej-com-br.example": ("clientej", "hub"),
    "clientef-com.example": ("clientei", "hub"),
    "psite15-com.example": ("psite15", "hub"),
    "clientel": ("evento_exemplo",),
    "institutogentil-com.example": ("instituto",),
}
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


def _resend(de, para, assunto, html, texto, responder_para, anexos, tags, cabecalhos, idempotencia, chave, timeout, transporte):
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


def _lista(para):
    return [para] if isinstance(para, str) else list(para)


def _mime(de, para, assunto, html, texto, responder_para, anexos, cabecalhos):
    m = email.message.EmailMessage()
    m["From"], m["To"], m["Subject"] = de, ", ".join(_lista(para)), assunto
    if responder_para:
        m["Reply-To"] = ", ".join(_lista(responder_para))
    for k, v in (cabecalhos or {}).items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(f"cabeçalho {k} não é texto")
        if k in m:
            raise ValueError(f"cabeçalho {k} repetido")
        m[k] = v
    if texto:
        m.set_content(texto)
        if html:
            m.add_alternative(html, subtype="html")
    else:
        m.set_content(html or "", subtype="html" if html else "plain")
    for n, a in enumerate(anexos or [], 1):
        if not isinstance(a, dict) or not a.get("content"):
            raise ValueError(f"anexo {n} sem content")
        tipo = a.get("content_type") or "application/octet-stream"
        principal, _, sub = tipo.partition("/")
        m.add_attachment(base64.b64decode(a["content"]), maintype=principal, subtype=sub or "octet-stream",
                         filename=a.get("filename") or "anexo")
    return m.as_bytes(policy=email.policy.SMTP)


def _dominio(de):
    """Domínio do remetente (`Nome <user1@example.com>` ou `user1@example.com`), minúsculo, ou None."""
    endereco = email.utils.parseaddr(de or "")[1]
    return endereco.rpartition("@")[2].strip().lower() or None if "@" in endereco else None


def _chave_do_app(de):
    """Credenciais SES do app dono do domínio de `de`: dict com as chaves do arquivo, ou None (cai na chave geral)."""
    apps = APPS_POR_DOMINIO.get(_dominio(de))
    if not apps:
        return None
    exclusiva = os.environ.get("SES_APPS_DIR", "").strip()
    pastas = [exclusiva] if exclusiva else [Path.home() / ".config" / "ses-apps", home_agentes() / ".config" / "ses-apps"]
    for app in apps:
        for pasta in pastas:
            dados = ler_env(Path(pasta) / f"{app}.env")
            if dados.get("SES_ACCESS_KEY_ID") and dados.get("SES_SECRET_ACCESS_KEY"):
                return dados
    return None


def _ses(de, para, assunto, html, texto, responder_para, anexos, tags, cabecalhos, cliente_ses, timeout=TIMEOUT):
    """SES sesv2. Não deduplica (sem ClientToken no send_email): reenviar manda de novo. Nunca lança."""
    if not (html or texto):
        return {"ok": False, "id": None, "status": None, "erro": "sem html nem texto", "esperarS": None}
    app = _chave_do_app(de) or {}
    acesso = app.get("SES_ACCESS_KEY_ID") or segredo("SES_ACCESS_KEY_ID", ARQUIVOS_CHAVE)
    secreto = app.get("SES_SECRET_ACCESS_KEY") or segredo("SES_SECRET_ACCESS_KEY", ARQUIVOS_CHAVE)
    try:
        if cliente_ses is not None:
            cli = cliente_ses if hasattr(cliente_ses, "send_email") else cliente_ses()
        else:
            if not (acesso and secreto):
                return {"ok": False, "id": None, "status": None, "erro": "sem SES_ACCESS_KEY_ID/SES_SECRET_ACCESS_KEY",
                        "esperarS": None}
            import boto3  # preguiçoso: o caminho Resend não exige boto3
            from botocore.config import Config
            # sem retry automático (o SES não deduplica: tentar de novo pode duplicar o e-mail) e com o timeout do chamador
            cfg = Config(connect_timeout=timeout, read_timeout=timeout, retries={"total_max_attempts": 1})
            cli = boto3.client("sesv2", region_name=app.get("SES_REGION") or segredo("SES_REGION", ARQUIVOS_CHAVE) or SES_REGION,
                               aws_access_key_id=acesso, aws_secret_access_key=secreto, config=cfg)
        pedido = {"FromEmailAddress": de, "Destination": {"ToAddresses": _lista(para)}}
        if tags:
            pedido["EmailTags"] = [{"Name": t["name"], "Value": t["value"]} for t in tags]
        conjunto = app.get("SES_CONFIGURATION_SET") or segredo("SES_CONFIGURATION_SET", ARQUIVOS_CHAVE)
        if conjunto:
            pedido["ConfigurationSetName"] = conjunto
        if anexos or cabecalhos:
            pedido["Content"] = {"Raw": {"Data": _mime(de, para, assunto, html, texto, responder_para, anexos, cabecalhos)}}
        else:
            corpo = {}
            if html:
                corpo["Html"] = {"Data": html, "Charset": "UTF-8"}
            if texto or not html:
                corpo["Text"] = {"Data": texto or "", "Charset": "UTF-8"}
            pedido["Content"] = {"Simple": {"Subject": {"Data": assunto, "Charset": "UTF-8"}, "Body": corpo}}
            if responder_para:
                pedido["ReplyToAddresses"] = _lista(responder_para)
        r = cli.send_email(**pedido)
        return {"ok": True, "id": r.get("MessageId"), "status": 200, "erro": None, "esperarS": None}
    except Exception as e:  # nunca lança; ClientError (boto) tem .response
        resp = getattr(e, "response", None)
        if isinstance(resp, dict) and "Error" in resp:
            cod = str(resp["Error"].get("Code") or "")
            msg = f"{cod}: {resp['Error'].get('Message') or ''}"
            status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode")
            espera = 1 if cod == "TooManyRequestsException" or cod.startswith("Throttling") else None
        else:
            msg, status, espera = f"{type(e).__name__}: {e}", None, None
        for segredo_ in (acesso, secreto):
            if segredo_:
                msg = msg.replace(segredo_, "***")
        return {"ok": False, "id": None, "status": status, "erro": msg[:CORPO_MAX], "esperarS": espera}


def enviar(de, para, assunto, html=None, texto=None, responder_para=None, anexos=None, tags=None, cabecalhos=None,
           idempotencia=None, chave=None, timeout=TIMEOUT, transporte=None, cliente_ses=None):
    """`para`: texto ou lista. Provedor por EMAIL_PROVEDOR (ses|resend; padrão PROVEDOR_PADRAO). `transporte(url,
    cabecalhos, corpo_bytes, timeout) -> (status, cabecalhos, corpo_bytes)` (Resend) e `cliente_ses` (objeto com
    `send_email` ou fábrica sem argumentos) são para teste. `idempotencia` só vale no Resend: o SES não deduplica.
    Nunca lança."""
    try:
        prov = (segredo("EMAIL_PROVEDOR", ARQUIVOS_CHAVE) or PROVEDOR_PADRAO).strip().lower()
        if prov not in PROVEDORES:
            return {"ok": False, "id": None, "status": None, "erro": f"EMAIL_PROVEDOR inválido: {prov}"[:CORPO_MAX],
                    "esperarS": None}
        if prov == "ses":
            return _ses(de, para, assunto, html, texto, responder_para, anexos, tags, cabecalhos, cliente_ses, timeout)
        return _resend(de, para, assunto, html, texto, responder_para, anexos, tags, cabecalhos, idempotencia, chave,
                       timeout, transporte)
    except Exception as e:
        return {"ok": False, "id": None, "status": None, "erro": f"{type(e).__name__}: {e}"[:CORPO_MAX], "esperarS": None}


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
