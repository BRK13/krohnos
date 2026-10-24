"""Orçamento global de memória das sessões e vigia de memória livre.

A servidor_hub caiu por OOM em 17/10 (19:49 UTC): 5 vagas × 4 contas × 6G prometiam 120G numa máquina de 31G, e o
`maquina_folgada()` só olhava MemAvailable na hora de abrir a vaga (5 builds começando juntos passavam todos).

- Orçamento: a soma das `MemoryMax` das sessões vivas (campo `memoria_max` do registro `rodando*.json`) mais a da nova
  tem de caber em ORCAMENTO (32G: reserva contábil, as sessões usam bem menos; o freio físico é o `MemoryHigh` do slice, 16G). Sem espaço, a vaga não abre (agenda.py).
  O comando vivo da fila `pesado` (scope próprio, 8G) também desconta: `reserva_pesado`.
- Vigia (cron a cada minuto, root): MemAvailable abaixo de 2G por 2 min avisa o orquestrador e pausa as vagas extras
  por 15 min (renovado enquanto a memória seguir baixa).

    python3 -m hub.memoria status     # orçamento, reservas e pausa
    python3 -m hub.memoria vigiar     # um giro do vigia (o cron)
"""
import argparse
import contextlib
import datetime as dt
import json
import os
import re
import sys

from hub import conta

UTC = dt.timezone.utc
ORCAMENTO = os.environ.get("HUB_MEMORIA_ORCAMENTO", "32G")  # reserva contábil: 4 sessões vivas usavam 1,4G juntas (19/10); o freio real é o MemoryHigh do slice + vigia
LIMITE_LIVRE = 2 * 1024 ** 3  # MemAvailable abaixo disso começa a contar
ESPERA_ALERTA = dt.timedelta(minutes=2)
PAUSA = dt.timedelta(minutes=15)
RESERVA_PESADO = "/tmp/nimbus-pesado.reserva"  # mesmo caminho de hub.pesado.RESERVA (pesado não importa memoria)
_UNIDADES = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def bytes_de(texto):
    """'6G' -> 6442450944 (formato do MemoryMax do systemd)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*", str(texto).upper())
    if not m:
        raise ValueError(f"tamanho inválido: {texto!r}")
    return int(float(m[1]) * _UNIDADES[m[2]])


def reservado(registros, padrao):
    """Soma das MemoryMax dos registros vivos; registro antigo, sem o campo, vale `padrao` (bytes)."""
    return sum(int(r.get("memoria_max") or padrao) for r in registros)


def reserva_pesado(arq=RESERVA_PESADO):
    """Bytes que o comando vivo da fila `pesado` reservou (scope próprio); 0 sem comando vivo."""
    try:
        with open(arq, encoding="utf-8") as f:
            r = json.load(f)
        os.kill(int(r["pid"]), 0)
        return int(r["memoria_max"] if isinstance(r["memoria_max"], int) else bytes_de(r["memoria_max"]))
    except PermissionError:  # o processo existe (de outro usuário)
        return bytes_de(r["memoria_max"]) if isinstance(r["memoria_max"], str) else int(r["memoria_max"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def cabe(registros, novo, padrao, orcamento=None, extra=0):
    """A sessão nova (MemoryMax `novo`, bytes) cabe no orçamento junto com as vivas e o `extra` (pesado vivo)?"""
    return reservado(registros, padrao) + extra + novo <= bytes_de(orcamento or ORCAMENTO)


def _pasta(base_dados=None):
    return conta.pasta(base_dados) / "memoria"


def _ler(arq):
    with contextlib.suppress(OSError, ValueError):
        return json.loads(arq.read_text(encoding="utf-8"))
    return None


def _gravar(arq, obj):
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_name(f".{arq.name}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o644)  # o vigia roda como root; as contas (usuário claude) só leem
    os.replace(tmp, arq)


def pausa_ativa(base_dados=None, quando=None):
    """{'desde', 'ate'} se as vagas extras estão pausadas por falta de memória; None se não."""
    p = _ler(_pasta(base_dados) / "pausa.json")
    try:
        if p and conta.de_iso(p["ate"]) > (quando or conta.agora()):
            return p
    except (KeyError, ValueError, TypeError):
        pass
    return None


def disponivel(meminfo="/proc/meminfo"):
    """MemAvailable em bytes; None sem como medir."""
    try:
        with open(meminfo, encoding="utf-8") as f:
            return next(int(l.split()[1]) * 1024 for l in f if l.startswith("MemAvailable:"))
    except (OSError, StopIteration, ValueError):
        return None


def vigiar(base_dados=None, quando=None, livre=None, avisar_fn=None):
    """Um giro: memória livre abaixo do limite por ESPERA_ALERTA chama o orquestrador e pausa as vagas extras.
    Devolve 'ok', 'baixa' (contando), 'pausada' ou 'alerta' (acabou de avisar)."""
    quando = quando or conta.agora()
    livre = disponivel() if livre is None else livre
    if livre is None:
        return "ok"
    d = _pasta(base_dados)
    estado = _ler(d / "vigia.json") or {}
    if livre >= LIMITE_LIVRE:
        if estado:
            (d / "vigia.json").unlink(missing_ok=True)
        return "ok"
    desde = conta.de_iso(estado["abaixo_desde"]) if estado.get("abaixo_desde") else quando
    if quando - desde < ESPERA_ALERTA:
        _gravar(d / "vigia.json", {"abaixo_desde": conta.iso(desde)})
        return "baixa"
    ja_pausada = pausa_ativa(base_dados, quando)
    _gravar(d / "pausa.json", {"desde": (ja_pausada or {}).get("desde") or conta.iso(quando),
                               "ate": conta.iso(quando + PAUSA)})
    _gravar(d / "vigia.json", {"abaixo_desde": conta.iso(desde)})
    if ja_pausada:
        return "pausada"
    (avisar_fn or conta.avisar)(
        f"Memória da servidor_hub abaixo de {LIMITE_LIVRE // 1024 ** 3} GB há {int((quando - desde).total_seconds() // 60)} min "
        f"({livre / 1024 ** 3:.1f} GB livres). Vagas extras pausadas por {int(PAUSA.total_seconds() // 60)} min; "
        "veja `python3 -m hub.monitor` e `ps --sort=-rss` antes de o kernel matar algo (KRN-1125).",
        tipo="tecnico", origem="vigia-memoria")
    return "alerta"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hub.memoria", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("status", "vigiar"))
    args = ap.parse_args(argv)
    if args.cmd == "vigiar":
        print(conta.iso(conta.agora()), vigiar())
        return 0
    from hub import agenda
    regs = [r for r in agenda.rodando_todas().values()]
    padrao = bytes_de(agenda.MEMORIA_MAX)
    print(f"orçamento {ORCAMENTO}; reservado {reservado(regs, padrao) / 1024 ** 3:.1f}G em {len(regs)} sessão(ões); "
          f"pausa de vagas extras: {pausa_ativa() or 'não'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
