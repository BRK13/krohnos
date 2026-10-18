"""Gabarito da busca da biblioteca, como no ClienteJ: 30 issues reais → as receitas que deveriam aparecer
(tests/biblioteca_gabarito.json; 21 com receita esperada e 9 em que o certo é não injetar nada). Mede precisão@3 e recall@3
contra o índice vivo e acha o limiar de cosseno que dá a melhor precisão sem perder recall.

    python3 -m hub.biblioteca_indice gabarito [--detalhe]
"""
import json
import sys
from pathlib import Path

from hub import biblioteca as B, biblioteca_indice as I

ARQ = Path(__file__).resolve().parent.parent / "tests" / "biblioteca_gabarito.json"
PROJETOS_NOME = {"Cliente C": "asite01", "ClienteJ": "clientej", "Cliente A": "fsite05", "Cliente F": "clientei",
                 "Cliente G": "psite13", "ClienteE & Moraes": "clientee", "Nimbus": "hub", "Cliente P": "apartamento"}


def carregar():
    return json.loads(ARQ.read_text(encoding="utf-8"))


def _caminhos(slug, todas):
    return [c for c in todas if c.endswith(f"/{slug}.md")]  # o mesmo nome pode existir em dois temas: valem os dois


def avaliar(casos=None, r=None, limiar=None, buscar=None):
    """{"precisao", "recall", "falsos_disparos", "casos": [...]}: precisão = acertos / receitas devolvidas (todas as
    issues), recall = acertos / min(3, esperadas) nas issues com receita esperada; falso disparo = issue sem receita esperada
    que recebeu alguma. limiar: cosseno mínimo (None = o do módulo)."""
    casos = casos if casos is not None else carregar()
    todas = [x["caminho"] for x in B.receitas(r)]
    buscar = buscar or (lambda c: I.buscar(I.consulta_da_issue(c["titulo"], c["descricao"], c["labels"]),
                                           None, None, limite=10))
    lim = I.LIMIAR_COS if limiar is None else limiar
    acertos = devolvidas = falsos = 0
    rec, n_rec, saida = 0.0, 0, []
    for c in casos:
        esperado = {p for s in c["esperado"] for p in _caminhos(s, todas)}
        dev = [s for s in buscar(c) if s["cos"] >= lim][:I.LIMITE_DESPACHO]
        hit = [s["caminho"] for s in dev if s["caminho"] in esperado]
        devolvidas += len(dev)
        acertos += len(hit)
        if esperado:
            rec += len(hit) / min(I.LIMITE_DESPACHO, len(esperado))
            n_rec += 1
        elif dev:
            falsos += 1
        saida.append({"issue": c["issue"], "devolvidas": [(s["caminho"], round(s["cos"], 2)) for s in dev], "acertos": hit,
                      "esperadas": sorted(esperado)})
    return {"precisao": acertos / devolvidas if devolvidas else 1.0, "recall": rec / n_rec if n_rec else 1.0,
            "falsos_disparos": falsos, "devolvidas": devolvidas, "casos": saida}


def main(argv=None):
    argv = argv or []
    if not I.disponivel():
        print("índice vazio: rode `python3 -m hub.biblioteca_indice atualizar`")
        return 2
    casos = carregar()
    cache = {}

    def buscar(c):  # uma consulta por caso (a chamada de embedding é a parte lenta); o limiar é aplicado depois
        if c["issue"] not in cache:
            cache[c["issue"]] = I.buscar(I.consulta_da_issue(c["titulo"], c["descricao"], c["labels"]), limite=10)
        return cache[c["issue"]]

    print("limiar  precisão@3  recall@3  falsos disparos  receitas injetadas")
    for lim in (0.0, 0.30, 0.35, 0.38, 0.40, 0.42, 0.44, 0.46, 0.48, 0.50, 0.55):
        m = avaliar(casos, limiar=lim, buscar=buscar)
        print(f"{lim:5.2f}   {m['precisao']:9.2f}  {m['recall']:8.2f}  {m['falsos_disparos']:>9}/{sum(1 for c in casos if not c['esperado'])}  {m['devolvidas']:>8}")
    m = avaliar(casos, buscar=buscar)
    print(f"\nno limiar do módulo ({I.LIMIAR_COS}): precisão@3 {m['precisao']:.2f}, recall@3 {m['recall']:.2f}")
    if "--detalhe" in argv:
        for c in m["casos"]:
            print(c["issue"], "OK" if c["acertos"] or not c["esperadas"] and not c["devolvidas"] else "..", "devolveu:",
                  c["devolvidas"], "| esperava:", [e.split("/")[-1][:-3] for e in c["esperadas"]])
    return 0
