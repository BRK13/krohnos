"""Eventos do SES via SNS (F4): o configuration set `hub` publica no tópico `ses-hub-eventos` (us-east-2) e o SNS faz
POST HTTPS em `/hooks/ses` (deploy/linear-webhook.py). Aqui: verificação da assinatura SNS, filtro de tópico, confirmação de
assinatura e gravação de uma linha JSONL por evento em `<dados>/ses-eventos/AAAA-MM-DD.jsonl`. Só biblioteca padrão e `cryptography`.

Tópico (`TOPICOS_PERMITIDOS()`): `SES_SNS_TOPIC_ARN` (ambiente ou segredo do hub; vários ARNs separados por vírgula) é
OBRIGATÓRIO. Vazio = recusa tudo (403) e registra um aviso sem valor, porque a assinatura SNS só prova que a mensagem veio
da AWS, não de qual conta: qualquer um cria um tópico de mesmo nome na própria conta. Com valor, só os ARNs listados passam.

Nunca grava segredo, `Token`, `SubscribeURL`, `SigningCertURL` nem o corpo bruto: só os campos resumidos de `linha_evento`.
`suprimidos.json` (mesma pasta) só registra Bounce Permanent e Complaint; não bloqueia envio nesta tarefa.
"""
import base64
import json
import http.client
import os
import re
import sys
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

RE_HOST_SNS = re.compile(r"sns\.[a-z0-9-]+\.amazonaws\.com")
CAMPOS = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
    "UnsubscribeConfirmation": ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"),
}
CERT_MAX = 20_000
TIMEOUT = 10
CACHE_MAX = 8
_CACHE_CERT = {}


class _SemRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_ABRIR = urllib.request.build_opener(_SemRedirect).open
_TRAVA = threading.Lock()


def url_sns_valida(url):
    """https e host `sns.<regiao>.amazonaws.com`, sem usuário nem porta."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
        return (u.scheme == "https" and bool(RE_HOST_SNS.fullmatch(u.hostname or "")) and u.port in (None, 443)
                and u.username is None)
    except (ValueError, TypeError, AttributeError):
        return False


def baixar_cert_padrao(url):
    """PEM do certificado (timeout de 10 s, no máximo 20 kB, cache em memória por URL). Só chamar com URL já validada."""
    if url in _CACHE_CERT:
        return _CACHE_CERT[url]
    with _ABRIR(url, timeout=TIMEOUT) as r:
        pem = r.read(CERT_MAX + 1)
    if len(pem) > CERT_MAX:
        raise ValueError("certificado grande demais")
    x509.load_pem_x509_certificate(pem)  # só guarda PEM que é certificado
    if len(_CACHE_CERT) >= CACHE_MAX:
        _CACHE_CERT.pop(next(iter(_CACHE_CERT)))
    _CACHE_CERT[url] = pem
    return pem


def texto_assinado(msg):
    """String canônica da doc AWS: `Campo\\nvalor\\n` na ordem do tipo, só os campos presentes. None se o tipo for desconhecido."""
    campos = CAMPOS.get(msg.get("Type"))
    if not campos:
        return None
    return "".join(f"{c}\n{msg[c]}\n" for c in campos if c in msg and msg[c] is not None)


def verificar_sns(msg, baixar_cert=None):
    """True só se a assinatura SNS (v1 = SHA1, v2 = SHA256, PKCS1v15) bate com o certificado do `SigningCertURL`, que precisa
    ser https em host SNS (senão recusa sem baixar nada). Qualquer falha devolve False."""
    try:
        if not isinstance(msg, dict):
            return False
        url = msg.get("SigningCertURL")
        texto = texto_assinado(msg)
        algo = {"1": hashes.SHA1, "2": hashes.SHA256}.get(str(msg.get("SignatureVersion")))  # noqa: S303
        if not (isinstance(url, str) and url_sns_valida(url) and texto is not None and algo and msg.get("Signature")):
            return False
        pem = (baixar_cert or baixar_cert_padrao)(url)
        chave = x509.load_pem_x509_certificate(pem if isinstance(pem, bytes) else pem.encode()).public_key()
        chave.verify(base64.b64decode(msg["Signature"]), texto.encode("utf-8"), padding.PKCS1v15(), algo())
        return True
    except (InvalidSignature, ValueError, TypeError, OSError, KeyError, AttributeError, http.client.HTTPException):
        return False


def TOPICOS_PERMITIDOS():  # noqa: N802
    """ARNs aceitos de `SES_SNS_TOPIC_ARN`; conjunto vazio = vale o padrão (ver docstring do módulo)."""
    from hub.comum import env
    bruto = env.segredo("SES_SNS_TOPIC_ARN") or ""
    return {a.strip() for a in bruto.split(",") if a.strip()}


def topico_ok(arn):
    if not isinstance(arn, str):
        return False
    permitidos = TOPICOS_PERMITIDOS()
    if not permitidos:
        print("ses_eventos: SES_SNS_TOPIC_ARN não definido, recusando tudo", file=sys.stderr, flush=True)
        return False
    return arn in permitidos


def _dados():
    import os
    from hub import data
    return Path(os.environ.get("HUB_DATA") or data()) / "ses-eventos"


def confirmar_padrao(url):
    with _ABRIR(url, timeout=TIMEOUT) as r:
        r.read(1000)


def linha_evento(msg, agora):
    """Linha JSONL resumida da Notification do SNS (ou None se o Message não for JSON de evento do SES)."""
    try:
        ses = json.loads(msg.get("Message") or "")
    except ValueError:
        return None
    if not isinstance(ses, dict):
        return None
    tipo = ses.get("eventType") or ses.get("notificationType")
    if not tipo:
        return None
    mail = ses.get("mail") or {}
    detalhe, dest = {}, list(mail.get("destination") or [])
    if tipo == "Bounce":
        b = ses.get("bounce") or {}
        detalhe = {"tipo": b.get("bounceType"), "subtipo": b.get("bounceSubType")}
        dest = [r.get("emailAddress") for r in b.get("bouncedRecipients") or [] if r.get("emailAddress")] or dest
        diag = (b.get("bouncedRecipients") or [{}])[0].get("diagnosticCode")
        if diag:
            detalhe["diagnostico"] = str(diag)[:200]
    elif tipo == "Complaint":
        c = ses.get("complaint") or {}
        detalhe = {"tipo": c.get("complaintFeedbackType")}
        dest = [r.get("emailAddress") for r in c.get("complainedRecipients") or [] if r.get("emailAddress")] or dest
    elif tipo == "Delivery":
        d = ses.get("delivery") or {}
        detalhe = {"smtp": str(d.get("smtpResponse") or "")[:120]}
    elif tipo == "DeliveryDelay":
        d = ses.get("deliveryDelay") or {}
        detalhe = {"tipo": d.get("delayType")}
    elif tipo == "Reject":
        detalhe = {"motivo": str((ses.get("reject") or {}).get("reason") or "")[:120]}
    elif tipo == "Rendering Failure":
        detalhe = {"erro": str((ses.get("failure") or {}).get("errorMessage") or "")[:200]}
    detalhe = {k: v for k, v in detalhe.items() if v}
    return {"recebido": agora.isoformat(timespec="seconds"), "tipo": tipo, "messageId_ses": mail.get("messageId"),
            "destinatarios": dest, "detalhe": detalhe, "sns_message_id": msg.get("MessageId")}


def _ja_gravado(pasta, sns_id, agora):
    for d in (agora, agora - timedelta(days=1)):  # SNS reenvia em minutos/horas; hoje e ontem bastam
        arq = pasta / f"{d:%Y-%m-%d}.jsonl"
        if arq.exists():
            for linha in arq.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(linha).get("sns_message_id") == sns_id:
                        return True
                except ValueError:
                    continue
    return False


def _suprimir(pasta, linha):
    permanente = linha["tipo"] == "Complaint" or (linha["tipo"] == "Bounce" and linha["detalhe"].get("tipo") == "Permanent")
    if not permanente:
        return
    arq = pasta / "suprimidos.json"
    try:
        atual = json.loads(arq.read_text(encoding="utf-8"))
    except FileNotFoundError:
        atual = {}  # só o arquivo ausente começa vazio; ilegível ou corrompido interrompe (o SNS reenvia), nunca sobrescreve
    for e in linha["destinatarios"]:
        atual[e.lower()] = {"motivo": linha["tipo"], "desde": atual.get(e.lower(), {}).get("desde") or linha["recebido"]}
    tmp = arq.with_name(arq.name + ".tmp")
    tmp.write_text(json.dumps(atual, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, arq)


def tratar(corpo, baixar_cert=None, confirmar=None, pasta=None, agora=None):
    """(status_http, info). 400 JSON inválido; 403 assinatura ou tópico recusado; 200 demais (confirmação feita, evento gravado
    ou repetido). `baixar_cert`, `confirmar` (GET do SubscribeURL), `pasta` e `agora` são injetáveis para teste."""
    try:
        msg = json.loads(corpo)
    except ValueError:
        return 400, "json invalido"
    if not isinstance(msg, dict):
        return 400, "json invalido"
    if not verificar_sns(msg, baixar_cert):
        return 403, "assinatura invalida"
    if not topico_ok(msg.get("TopicArn")):
        return 403, "topico nao permitido"
    tipo = msg.get("Type")
    if tipo == "SubscriptionConfirmation":
        url = msg.get("SubscribeURL")
        if not url_sns_valida(url):
            return 403, "SubscribeURL invalida"
        try:
            (confirmar or confirmar_padrao)(url)
        except Exception as e:  # noqa: BLE001
            return 502, f"confirmacao falhou: {type(e).__name__}"
        return 200, "assinatura confirmada"
    if tipo != "Notification":
        return 200, "ignorado"
    agora = agora or datetime.now(timezone.utc)
    linha = linha_evento(msg, agora)
    if linha is None:
        return 200, "ignorado"
    pasta = Path(pasta) if pasta else _dados()
    with _TRAVA:
        pasta.mkdir(parents=True, exist_ok=True)
        sns_id = linha["sns_message_id"]
        if sns_id and _ja_gravado(pasta, sns_id, agora):
            return 200, "repetido"
        _suprimir(pasta, linha)  # antes do marcador (a linha): falha aqui deixa o reenvio do SNS refazer tudo
        with open(pasta / f"{agora:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(linha, ensure_ascii=False) + "\n")
    return 200, "gravado"


def resumo(dias=1, pasta=None, agora=None):
    """Contagem por tipo dos eventos dos últimos `dias` dias (hoje conta como 1): {"Delivery": 3, "Bounce": 1}."""
    agora = agora or datetime.now(timezone.utc)
    pasta = Path(pasta) if pasta else _dados()
    cont = {}
    for i in range(max(int(dias), 1)):
        arq = pasta / f"{agora - timedelta(days=i):%Y-%m-%d}.jsonl"
        if not arq.exists():
            continue
        for linha in arq.read_text(encoding="utf-8").splitlines():
            try:
                t = json.loads(linha).get("tipo")
            except ValueError:
                continue
            if t:
                cont[t] = cont.get(t, 0) + 1
    return cont
