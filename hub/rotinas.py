"""Rotinas agendadas do contêiner (serviço `agenda`): decide, minuto a minuto, o que rodar.

Fontes: (1) rotinas dos plugins (`plugins.rotinas()`), cada uma com horário cron em krohnos.toml [rotinas] horarios
{nome = "m h dom mon dow"} (UTC; sem horário, não roda sozinha); (2) opcional, um arquivo no formato do crontab do sistema
(`[rotinas] cron`, ex. deploy/nimbus.cron): só as linhas que chamam deploy/rotina.sh, sem o usuário e sem o redirecionamento
de log (a saída vai para o log do contêiner; a tela Rotinas lê o arquivo do redirecionamento para mostrar o último resultado).
`[rotinas] desligadas` (lista de nomes, gravada pela tela /config/rotinas) impede o disparo sozinho; sem a chave nada muda. Uso: python3 -m hub.rotinas [--agora AAAA-MM-DDTHH:MM] (roda as devidas).
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import time
import sys
from pathlib import Path

from hub import RAIZ, config, plugins
from hub import data as dados_dir
from hub.tarefas import Cron

RE_LINHA = re.compile(r"^(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+\S+\s+(\S*/deploy/rotina\.sh\s.*)$")
RE_LOG = re.compile(r"\s*>>?\s*(\S+)(\s+2>&1)?\s*$")
PULADA = 79  # rotina.sh com FORCAR=1 e a trava ocupada (sem FORCAR sai 0); 79 e não 75 para não colidir com código de rotina de verdade
SEMANA = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")


def _campo(expr, valor, minimo, maximo):
    """Um campo do cron (listas, faixas, passos, *) casa com o valor?"""
    for parte in expr.split(","):
        faixa, _, passo = parte.partition("/")
        passo = int(passo) if passo else 1
        if faixa == "*":
            a, b = minimo, maximo
        elif "-" in faixa:
            a, b = (int(x) for x in faixa.split("-"))
        else:
            a = int(faixa)
            b = maximo if passo > 1 else a
        if a <= valor <= b and (valor - a) % passo == 0:
            return True
    return False


def casa(expr, agora):
    """True se a expressão cron de 5 campos (UTC) cobre este minuto. Domingo é 0 ou 7."""
    campos = expr.split()
    if len(campos) != 5:
        raise ValueError(f"horário cron inválido: {expr!r}")
    m, h, dia, mes, sem = campos
    dow = (agora.weekday() + 1) % 7
    dia_ok, sem_ok = _campo(dia, agora.day, 1, 31), (_campo(sem, dow, 0, 6) or (dow == 0 and _campo(sem, 7, 0, 7)))
    if dia != "*" and sem != "*":  # regra do cron: os dois restritos = qualquer um
        dia_sem = dia_ok or sem_ok
    else:
        dia_sem = dia_ok and sem_ok
    return _campo(m, agora.minute, 0, 59) and _campo(h, agora.hour, 0, 23) and _campo(mes, agora.month, 1, 12) and dia_sem


def chave_tarefa(nome):
    """Como a tarefa do Claude entra em `rotinas.desligadas`: "tarefa:<nome>". O prefixo existe porque rotina e tarefa
    podem ter o mesmo nome (asite01-prospeccao e fsite06-gmb têm os dois) e desligar uma não pode desligar a outra."""
    return f"tarefa:{nome}"


def desligadas():
    """Chaves desligadas pela tela Rotinas (`rotinas.desligadas`, na sobreposição do painel): o nome da rotina, ou
    `chave_tarefa(nome)` para tarefa do Claude. Sem a chave, vazio: tudo dispara como sempre."""
    valor = config.get("rotinas.desligadas")
    return {str(n) for n in valor} if isinstance(valor, (list, tuple)) else set()


def catalogo(todas=False):
    """[{nome, horario, comando, fonte, log}] de tudo que a agenda pode disparar. `fonte`: "plugin" (horário em
    rotinas.horarios) ou "sistema" (linha de rotinas.cron); `log` é o arquivo do redirecionamento da linha (ou None).
    Os horários são todos em UTC. `todas=True` inclui também a rotina de plugin sem horário (horario None: só roda a pedido)."""
    itens = []
    horarios = config.get("rotinas.horarios")
    for nome, comando in plugins.rotinas().items():
        if nome in horarios or todas:
            itens.append({"nome": nome, "horario": horarios.get(nome), "comando": comando, "fonte": "plugin", "log": None})
    cron = config.get("rotinas.cron")
    if cron and Path(cron).expanduser().is_file():
        for linha in Path(cron).expanduser().read_text(encoding="utf-8").splitlines():
            m = RE_LINHA.match(linha.strip())
            if m:
                bruto = m.group(2)
                lg = RE_LOG.search(bruto)
                comando = re.sub(r"^\S*/deploy/rotina\.sh", "deploy/rotina.sh", RE_LOG.sub("", bruto))
                partes = comando.split()
                itens.append({"nome": partes[1] if len(partes) > 1 else comando, "horario": m.group(1), "comando": comando,
                              "fonte": "sistema", "log": lg.group(1) if lg else None})
    return itens


def agendadas():
    """[(horário, comando)] de tudo que a agenda pode disparar."""
    return [(i["horario"], i["comando"]) for i in catalogo()]


def proxima(expr, agora, fuso=dt.timezone.utc):
    """Próximo instante (minuto cheio, estritamente depois de `agora`) em que o cron de 5 campos bate, no `fuso`
    (padrão UTC; as tarefas do Claude usam BRT). `agora` sem fuso vale como já estando no `fuso`. Função pura.
    ValueError se o cron é inválido; None se nunca bate (ex.: 30 de fevereiro)."""
    c = Cron(expr)
    t = agora.astimezone(fuso) if agora.tzinfo else agora.replace(tzinfo=fuso)
    t = t.replace(second=0, microsecond=0)
    horas, minutos = sorted(c.hora), sorted(c.min)
    for passo in range(366 * 5):
        dia = (t + dt.timedelta(days=passo)).date()
        dia_ok, sem_ok = dia.day in c.dia, (dia.isoweekday() % 7) in c.sem
        if dia.month not in c.mes or not ((dia_ok and sem_ok) if (c.dia_livre or c.sem_livre) else (dia_ok or sem_ok)):
            continue
        for h in horas:
            for m in minutos:
                cand = dt.datetime(dia.year, dia.month, dia.day, h, m, tzinfo=fuso)
                if cand > t:
                    return cand
    return None


def rotulo_proxima(prox, agora):
    """"qui 08:00" (até 6 dias à frente), senão "22/10 08:00"; "nunca" sem próximo."""
    if prox is None:
        return "nunca"
    a = agora.astimezone(prox.tzinfo) if agora.tzinfo else agora.replace(tzinfo=prox.tzinfo)
    if (prox.date() - a.date()).days < 7:
        return f"{SEMANA[prox.weekday()]} {prox:%H:%M}"
    return f"{prox:%d/%m} {prox:%H:%M}"


def _ts(texto):
    try:
        return dt.datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


RE_FIM = re.compile(r"^(\S+) (\S+): fim, código (\d+)\s*$")
RE_INICIO = re.compile(r"^(\S+) (\S+): início\b")


def ultimo(item, base_dados=None, max_bytes=200_000):
    """Último resultado de uma rotina: {ok, em, duracao_s, log, origem} ou None.
    Fonte 1: o log da linha do cron (deploy/rotina.sh escreve "<UTC> <nome>: início" e "<UTC> <nome>: fim, código N").
    Fonte 2: $HUB_DATA/rotinas/falhas.jsonl (só falhas; é o que há para rotina de plugin, cuja saída vai para o log do
    contêiner). Sem nenhuma das duas, None: não há registro de sucesso."""
    nome, log = item["nome"], item.get("log")
    achado = None
    if log:
        try:
            with open(Path(log).expanduser(), "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - max_bytes))
                linhas = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            linhas = []
        inicio = None
        for linha in linhas:
            m = RE_INICIO.match(linha)
            if m and m.group(2) == nome:
                inicio = _ts(m.group(1))
                continue
            m = RE_FIM.match(linha)
            if m and m.group(2) == nome:
                fim = _ts(m.group(1))
                if fim:
                    achado = {"ok": m.group(3) == "0", "em": fim, "origem": "log", "log": log,
                              "duracao_s": round((fim - inicio).total_seconds(), 1) if inicio and fim >= inicio else None}
                inicio = None
    falha = None
    try:
        arq = Path(base_dados or dados_dir()).expanduser() / "rotinas" / "falhas.jsonl"
        for linha in arq.read_text(encoding="utf-8").splitlines()[-500:]:
            try:
                r = json.loads(linha)
            except ValueError:
                continue
            if r.get("nome") == nome and _ts(r.get("em")):
                falha = {"ok": False, "em": _ts(r["em"]), "origem": "falhas", "log": log, "duracao_s": None}
    except OSError:
        pass
    manual = _manual(nome, base_dados)
    candidatos = [r for r in (achado, falha, manual, _raiz(nome, base_dados)) if r]
    return max(candidatos, key=lambda r: r["em"]) if candidatos else None


def pasta_manual(base_dados=None):
    return Path(base_dados or dados_dir()) / "rotinas" / "manual"


def _manual(nome, base_dados=None):
    """Resultado do último "rodar agora" feito pelo painel (executar_capturado), ou None."""
    try:
        r = json.loads((pasta_manual(base_dados) / f"{nome}.json").read_text(encoding="utf-8"))
        return {"ok": bool(r["ok"]), "em": _ts(r["fim"]), "duracao_s": r.get("duracao_s"), "origem": "manual",
                "log": r.get("log"), "codigo": r.get("codigo"), "pulada": bool(r.get("pulada"))}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _arq_pedido(nome, base_dados=None):
    return pasta_manual(base_dados) / f"{nome}.pedido.json"


def registrar_pedido(nome, pid, base_dados=None):
    """Guarda qual pedido ao root é o "rodar agora" mais novo desta rotina (a tela procura o resultado dele)."""
    pasta = pasta_manual(base_dados)
    pasta.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pasta, prefix=f".{nome}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"id": pid, "em": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, _arq_pedido(nome, base_dados))


def _raiz(nome, base_dados=None):
    """Resultado do último pedido "rodar agora" ao root (hub.raiz); sem resultado ainda: {"pendente": id, ok None}."""
    try:
        ped = json.loads(_arq_pedido(nome, base_dados).read_text(encoding="utf-8"))
        pid = ped["id"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    from hub import raiz
    try:
        reg = raiz.resultado(pid, base_dados) if isinstance(pid, str) else None
    except (OSError, ValueError, TypeError):
        reg = None
    if not reg:
        return {"ok": None, "pendente": pid, "em": _ts(ped.get("em")) or dt.datetime.now(dt.timezone.utc), "duracao_s": None,
                "origem": "raiz", "log": None, "pulada": False}
    ini = _ts(reg.get("em"))
    dur = reg.get("duracao_s")
    fim = ini + dt.timedelta(seconds=dur) if ini and dur is not None else ini
    return {"ok": reg.get("codigo") == 0, "em": fim or dt.datetime.now(dt.timezone.utc), "duracao_s": dur, "origem": "raiz", "log": None,
            "pulada": reg.get("codigo") == PULADA, "pedido": pid, "codigo": reg.get("codigo")}


def log_manual(nome, base_dados=None):
    return pasta_manual(base_dados) / f"{nome}.log"


def executar_capturado(item, base_dados=None, timeout=3600):
    """Roda a rotina agora, com FORCAR=1 (ignora o "desligada no painel" do rotina.sh), junta stdout e stderr num log em
    <dados>/rotinas/manual/<nome>.log e grava o desfecho (ok, código, duração) em <nome>.json, atomicamente. Devolve o
    desfecho. Falha ao subir o processo (ou estouro do tempo) também vira desfecho com ok False, nunca fica calada."""
    nome = item["nome"]
    pasta = pasta_manual(base_dados)
    pasta.mkdir(parents=True, exist_ok=True)
    log = log_manual(nome, base_dados)
    inicio = dt.datetime.now(dt.timezone.utc)
    t0 = time.monotonic()
    erro = None
    try:
        with open(log, "wb") as f:
            r = subprocess.run(["bash", "-c", item["comando"]], cwd=RAIZ, stdout=f, stderr=subprocess.STDOUT, timeout=timeout,
                               env={**os.environ, "FORCAR": "1"}, stdin=subprocess.DEVNULL)
        codigo = r.returncode
    except subprocess.TimeoutExpired:
        codigo, erro = 124, f"passou de {timeout} s"
    except OSError as ex:
        codigo, erro = 127, f"não consegui iniciar: {type(ex).__name__}"
        with open(log, "ab") as f:
            f.write(f"{erro}\n".encode())
    fim = dt.datetime.now(dt.timezone.utc)
    if codigo == PULADA:
        erro = "pulada: já estava rodando"
    desfecho = {"nome": nome, "ok": codigo == 0, "pulada": codigo == PULADA, "codigo": codigo, "inicio": inicio.isoformat(timespec="seconds"),
                "fim": fim.isoformat(timespec="seconds"), "duracao_s": round(time.monotonic() - t0, 1), "log": str(log), "erro": erro}
    fd, tmp = tempfile.mkstemp(dir=pasta, prefix=f".{nome}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(desfecho, f, ensure_ascii=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, pasta / f"{nome}.json")
    return desfecho


def preparar_captura(nome, base_dados=None):
    """No processo do painel, ANTES de soltar o filho: cria <dados>/rotinas/manual e abre o log. Sem permissão, levanta OSError
    com a mensagem (em português) que a tela mostra: nada de "rodando" eterno por erro que só o filho veria."""
    pasta = pasta_manual(base_dados)
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        return open(log_manual(nome, base_dados), "ab")
    except OSError as ex:
        import getpass
        try:
            quem = getpass.getuser()
        except Exception:  # noqa: BLE001
            quem = "<usuário do painel>"
        raise OSError(f"a pasta {pasta} não é gravável pelo usuário do painel ({type(ex).__name__}); crie-a com: "
                      f"sudo install -d -o {quem} {pasta}") from ex


def disparar_capturado(item, base_dados=None):
    """Põe `executar_capturado` para rodar solto (processo próprio, sobrevive ao painel). OSError (com mensagem pronta para a
    tela) se a pasta do log não é gravável ou o filho nem subiu. O stderr do filho vai para o mesmo log."""
    log = preparar_captura(item["nome"], base_dados)
    cmd = [sys.executable, "-m", "hub.rotinas", "capturar", item["nome"]] + (["--base", str(base_dados)] if base_dados else [])
    try:
        subprocess.Popen(cmd, cwd=RAIZ, start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    finally:
        log.close()


def disparar(comando):
    """Roda a linha da rotina agora (fora do minuto marcado). rotina.sh tem trava própria: dois ao mesmo tempo não colidem."""
    subprocess.Popen(["bash", "-c", comando], cwd=RAIZ, start_new_session=True)


def devidas(agora):
    """Comandos devidos neste minuto. Horário inválido de uma rotina é logado no stderr e só essa rotina fica de fora."""
    saida = []
    fora = desligadas()
    for i in catalogo():
        h, c = i["horario"], i["comando"]
        if i["nome"] in fora:
            continue
        try:
            if casa(h, agora):
                saida.append(c)
        except (ValueError, TypeError, AttributeError, ZeroDivisionError) as ex:  # nome (mon), */0, valor não-string, campos faltando
            print(f"rotinas: horário {h!r} ignorado ({c.split()[1] if len(c.split()) > 1 else c}): {ex}", file=sys.stderr)
    return saida


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["desligada"] and len(args) == 2:
        # (roda como root, chamado pelo rotina.sh: só lê rotinas.desligadas, uma chave permitida na sobreposição; nada de plugins.caminho
        # nem caminhos.*, que a sobreposição não define) deploy/rotina.sh pergunta antes de rodar: 0 = desligada no painel, 1 = ligada; qualquer outra coisa = roda como hoje
        try:
            return 0 if args[1] in desligadas() else 1
        except Exception:  # noqa: BLE001
            return 2
    if args[:1] == ["capturar"] and len(args) >= 2:
        item = next((i for i in catalogo(todas=True) if i["nome"] == args[1]), None)
        if item is None:
            print(f"rotinas: {args[1]!r} não está no catálogo", file=sys.stderr)
            return 1
        base = args[args.index("--base") + 1] if "--base" in args[2:] else None
        return 0 if executar_capturado(item, base)["ok"] else 1
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agora", help="AAAA-MM-DDTHH:MM em UTC (padrão: agora)")
    a = ap.parse_args(argv)
    agora = dt.datetime.fromisoformat(a.agora).replace(tzinfo=None) if a.agora else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for comando in devidas(agora):
        disparar(comando)
    return 0


if __name__ == "__main__":
    sys.exit(main())
