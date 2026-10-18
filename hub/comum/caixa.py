"""Caixa de avisos do WhatsApp: o escritor único do formato que o bot da asite01 (triagem-wpp) lê.

Um aviso é um arquivo `<caixa>/AAAAMMDDTHHMMSS-<pid>-<ns>.json` com `{texto, agora, tarefa?, arquivos?, para?}`, gravado em
`.tmp` e renomeado (o bot só lê .json: nunca pega pela metade). Com TAREFA_NOME, o aviso leva `tarefa` e uma linha vai
para `avisos-enviados.jsonl` (o recibo de pronto,, confere por ali). CLI: `python3 -m hub.avisar` (hub/avisar.py).
"""
import contextlib
import json
import os
import re
import time
from pathlib import Path

from hub.comum import datas

CAIXA = os.environ.get("CAIXA_AVISOS", "/opt/asite01-wpp/data/avisos")


def _pasta_dados(base_dados=None):
    from hub import data
    return Path(base_dados or data()) / "claude"


def _gravar_cru(texto, agora, caixa, arquivos, para):
    """Grava o aviso na caixa, sem passar pelo porteiro (o resumo da fila usa direto)."""
    caixa = Path(caixa or CAIXA)
    nome = caixa / f"{datas.agora():%Y%m%dT%H%M%S}-{os.getpid()}-{time.monotonic_ns() % 100000}"
    tmp = nome.with_suffix(".tmp")
    tarefa = os.environ.get("TAREFA_NOME")
    aviso = {"texto": texto, "agora": bool(agora)}
    if tarefa:
        aviso["tarefa"] = tarefa  # o recibo de pronto acha o aviso enviado por aqui
    if arquivos:
        aviso["arquivos"] = list(arquivos)
    if para:
        numero = re.sub(r"\D", "", str(para))
        if not numero:
            raise ValueError("`para` sem dígitos")
        aviso["para"] = numero
    tmp.write_text(json.dumps(aviso, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, nome.with_suffix(".json"))
    return nome.with_suffix(".json")


def _eh_a_caixa_do_dono(caixa):
    """O porteiro vale para a caixa de produção do bot; caixa de teste (pasta temporária) grava direto."""
    return caixa is None or Path(caixa).resolve() == Path(CAIXA).resolve()


def _desviar_tecnico(texto):
    from hub import debugger
    debugger.alerta_tecnico(texto[:1500])


def escrever(texto, agora=False, caixa=None, arquivos=None, para=None, base_dados=None, origem=None, tipo=None, pedido=False):
    """Grava o aviso e devolve o caminho do .json (None: o porteiro, hub/comum/porteiro.py, barrou, desviou ou pôs na fila).
    `arquivos`: caminhos relativos ao DATA_DIR do bot (ex. reels/<id>.mp4), mandados junto. `para`: número (55DDD...) que
    recebe no lugar do dono; só para destino que o dono autorizou (mensagem a outro número não passa pelo porteiro).
    `tipo="tecnico"` desvia ao orquestrador; `origem` nomeia o remetente no relatório; `pedido=True`: o dono pediu esta
    versão, vale mesmo havendo envio da issue no mesmo dia."""
    from hub.comum import porteiro
    if para:  # valida antes de decidir, como sempre
        if not re.sub(r"\D", "", str(para)):
            raise ValueError("`para` sem dígitos")
    if porteiro.eh_dono(para) and _eh_a_caixa_do_dono(caixa):
        caminho = porteiro.passar(texto, agora, caixa, arquivos, para, origem, tipo, pedido, base_dados,
                                  gravar=lambda t, a: _gravar_cru(t, agora, caixa, a, para), desviar=_desviar_tecnico)
    else:
        caminho = _gravar_cru(texto, agora, caixa, arquivos, para)
    tarefa = os.environ.get("TAREFA_NOME")
    if tarefa:
        with contextlib.suppress(OSError):  # o recibo de pronto confere que o aviso pedido saiu (ou foi tratado pelo porteiro)
            pasta = _pasta_dados(base_dados)
            pasta.mkdir(parents=True, exist_ok=True)
            with open(pasta / "avisos-enviados.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"em": datas.agora().astimezone(datas.UTC).isoformat(timespec="seconds"), "tarefa": tarefa,
                                    "texto": texto[:200]}, ensure_ascii=False) + "\n")
    return caminho


def descarregar(caixa=None, base_dados=None, quando=None):
    """A cada tick: manda o resumo único da fila do porteiro (madrugada, teto, versão do mesmo dia). Texto ou None."""
    from hub.comum import porteiro
    return porteiro.descarregar(caixa, base_dados, quando, gravar=lambda t, a: _gravar_cru(t, True, caixa, a, None))
