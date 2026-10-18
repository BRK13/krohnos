"""Vigia de "parou de andar": rotina de dado que bate ponto "ok" mas não traz nada novo há N dias.

O vigia do ClienteJ (coleta/batimento.py, tabela job_batimento) só acusa rotina calada ou com falha. O STJ ficou 8 dias
parado com o job dizendo "0 erros": rodava, não trazia nada. Aqui, por rotina, N dias (hub/vigia_novidade.json) sem a
contagem mudar, com o job vivo e ok, vira alarme para o orquestrador (diário de consertos, tentando) e entra na
lista "parado ou só código" do resumo diário.

    python3 -m hub.vigia_novidade          uma rodada (cron diário)

Entrada: $HUB_DATA/vigia/batimentos.json = {"gerado_em", "jobs": [{"job", "ultimo_em", "ok", "contagem" | "detalhe"}]}, uma linha da
job_batimento por job com o estoque, exportada na VPS do banco por coleta/vigia_estoques.py (clienteh, via
vps-vigia.sh) e puxada por ssh a cada rodada (o hub não lê o banco de produção). Job sem linha está desligado e é
ignorado. Feed ausente, inválido, com mais de 2 dias ou sem nenhum job configurado é falha de monitoramento (saída 1),
nunca sucesso sem verificar. `contagem` é o estoque acumulado
(progresso = subir). Sem ela, vale o primeiro número de itens do `detalhe` ("rodada: 1.234 acórdãos"), que é a quantidade
da rodada (progresso = maior que zero, então 100 por dia não alarma); erros e horários não são progresso. Estado em $HUB_DATA/vigia/novidade-estado.json.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from hub import RAIZ, consertos, data

UTC = dt.timezone.utc
VIVO = dt.timedelta(days=2)  # batimento mais velho que isso é do outro vigia (rotina calada), não "sem novidade"
REAVISO = dt.timedelta(hours=20)  # um aviso por dia enquanto continuar parado
FMT = "%Y-%m-%dT%H:%M:%SZ"
FEED_VELHO = dt.timedelta(days=2)  # exportador parado: o feed não prova nada
ORIGEM = "/var/lib/vigia-clientej/batimentos.json"  # na vps-nova
CMD_PUXAR = ["runuser", "-u", "orch", "--", "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "vps-nova",
             f"cat {ORIGEM}"]


def _t(iso):
    return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(UTC)


def contagem(j):
    if j.get("contagem") is not None:
        n = j["contagem"]
        return n if type(n) is int and n >= 0 else None
    # Só aceita quantidade de dados; "0 erros" não mede decisões coletadas.
    m = re.search(r"(?<![\d.:,])\b(\d{1,3}(?:\.\d{3})+|\d+)\s+"
                  r"(?:acórdãos|acordaos|decisões|decisoes|documentos|itens|registros|"
                  r"classificações|classificacoes|textos|embeddings)\b",
                  j.get("detalhe") or "", re.IGNORECASE)
    return int(m.group(1).replace(".", "")) if m else None


def avaliar(jobs, config, estado, agora):
    """Puro. Devolve ([{"job", "texto"}], novo_estado). Job fora da config é ignorado."""
    novo, alertas = {k: dict(v) for k, v in estado.items()}, []
    for j in jobs:
        cfg, n = config.get(j.get("job")), contagem(j)
        if not cfg or n is None:
            continue
        e = novo.setdefault(j["job"], {"contagem": n, "desde": agora.strftime(FMT)})
        vivo = j.get("ok") and dt.timedelta(0) <= agora - _t(j["ultimo_em"]) <= VIVO
        # `contagem` é estoque acumulado (progresso = subir); só `detalhe` é quantidade da rodada (progresso = trazer > 0).
        por_rodada = j.get("contagem") is None
        if vivo and (n > 0 if por_rodada else n > e.get("contagem", n)):
            e.update(contagem=n, desde=agora.strftime(FMT), parado=False)
            e.pop("avisado_em", None)
            continue
        # Queda no estoque não prova entrada de decisões novas. Preserva a
        # referência até superar a última contagem que demonstrou progresso.
        dias = (agora - _t(e["desde"])).days
        e["parado"] = False
        if not vivo or dias < cfg["dias"]:
            continue
        e["parado"] = True
        if e.get("avisado_em") and agora - _t(e["avisado_em"]) < REAVISO:
            continue
        e["avisado_em"] = agora.strftime(FMT)
        alertas.append({"job": j["job"], "texto": f"{j['job']} roda \"ok\" mas não traz nada novo há {dias} dias "
                                                  f"({'rodadas sem itens' if por_rodada else f'contagem parada em {n}'}; limite {cfg['dias']} dias)"})
    return alertas, novo


def _ler(arq, padrao):
    try:
        return json.loads(Path(arq).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def puxar(base, executar=subprocess.run):
    """Traz o feed da vps-nova para base/vigia/batimentos.json. Falha de ssh vira ValueError (monitoramento quebrado)."""
    try:
        r = executar(CMD_PUXAR, capture_output=True, timeout=60)
        if r.returncode != 0:
            raise OSError(r.stderr.decode(errors="replace")[-200:] or f"código {r.returncode}")
        json.loads(r.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ValueError(f"vigia não conseguiu puxar {ORIGEM} da vps-nova: {exc}") from exc
    arq = Path(base) / "vigia" / "batimentos.json"
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(".tmp")
    tmp.write_bytes(r.stdout)
    os.replace(tmp, arq)


def rodar(base, config, agora=None, registrar=consertos.registrar):
    base = Path(base)
    agora = agora or dt.datetime.now(UTC)
    feed = _ler(base / "vigia" / "batimentos.json", None)
    if not isinstance(feed, dict) or not isinstance(feed.get("jobs"), list):
        raise ValueError("vigia sem feed válido: vigia/batimentos.json; conferir coleta/vigia_estoques.py na vps-nova")
    try:
        idade = agora - _t(feed["gerado_em"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("vigia/batimentos.json sem gerado_em válido") from exc
    if idade > FEED_VELHO:
        raise ValueError(f"vigia/batimentos.json velho ({idade.days} dias): exportador da vps-nova parado")
    jobs = feed["jobs"]
    encontrados = set()
    for j in jobs:
        if not isinstance(j, dict):
            raise ValueError("vigia/batimentos.json: job inválido")
        if j.get("job") not in config:
            continue
        try:
            if contagem(j) is None or not isinstance(j.get("ok"), bool):
                raise ValueError("contagem ou ok ausente/inválido")
            if not dt.datetime.fromisoformat(str(j["ultimo_em"]).replace("Z", "+00:00")).tzinfo:
                raise ValueError("ultimo_em sem fuso")
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"vigia/batimentos.json: batimento inválido de {j['job']}") from exc
        encontrados.add(j["job"])
    if not encontrados:  # job sem linha está desligado (ok); feed sem nenhum dos configurados é exportador quebrado
        raise ValueError("vigia sem nenhum batimento configurado: " + ", ".join(sorted(config)))
    arq = base / "vigia" / "novidade-estado.json"
    alertas, estado = avaliar(jobs, config, _ler(arq, {}), agora)
    for a in alertas:
        registrar(quebrou=a["texto"], causa="a rotina bate ponto ok mas a contagem não anda: fonte parada, filtro novo ou "
                  "lote que não chega ao banco", conserto="investigar a fonte pelo orquestrador; acompanhar no resumo, sem WhatsApp",
                  estado="tentando", por="vigia-novidade", base_dados=str(base))
    # Só confirma o reaviso depois de entregar ao diário. Falha deve permitir nova tentativa.
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, arq)
    return alertas


def parados(base=None):
    """Linhas do resumo diário: rotinas hoje paradas."""
    estado = _ler(Path(base or data()) / "vigia" / "novidade-estado.json", {})
    return [f"- rotina {k}: roda ok, mas sem nada novo desde {_t(e['desde']):%d/%m} (contagem {e['contagem']})"
            for k, e in estado.items() if e.get("parado")]


def main():
    config = json.loads((Path(RAIZ) / "hub" / "vigia_novidade.json").read_text(encoding="utf-8"))
    try:
        puxar(data())
        alertas = rodar(data(), config)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for a in alertas:
        print(a["texto"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
