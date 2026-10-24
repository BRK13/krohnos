"""itens p1 esperando há mais de 10 min com conta ociosa. O `coordenar` mede todo minuto; passou de 0 por 15 min
vira incidente P1 para o orquestrador. Meta: 24 h sem nenhuma conta ociosa com item p1 esperando mais de 10 min.

    python3 -m hub.ociosidade            # mede agora (o `coordenar` já faz isso a cada minuto)
    python3 -m hub.ociosidade resumo     # últimas 24 h
"""
import datetime as dt
import json
import sys

from hub import conta

ESPERA_MIN = dt.timedelta(minutes=10)  # o item espera mais que isso...
PERSISTE_MIN = dt.timedelta(minutes=15)  # ...e o contador fica acima de 0 por isso, vira incidente
RETENCAO = dt.timedelta(days=8)


def _arq(base_dados=None, nome="ociosidade.json"):
    return conta.pasta(base_dados) / nome


def _ler(arq, padrao):
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def contar(base_dados=None, agora=None):
    """(itens p1 esperando há mais de 10 min, contas ociosas): só conta quando há conta ociosa. Item do Codex e `conta: api`
    não são das contas OAuth, então não entram."""
    from hub import agenda
    agora = agora or conta.agora()
    ociosas = [n for n in conta.contas_config() if agenda.conta_ociosa(n, base_dados, agora)]
    if not ociosas:
        return [], ociosas
    # INC-401: vaga da conta não implica espaço no orçamento global.
    # O despacho espera legitimamente quando as sessões vivas já o reservaram.
    if not agenda.memoria.cabe(list(agenda.rodando_todas(base_dados).values()),
                               agenda.memoria.bytes_de(agenda.MEMORIA_MAX),
                               agenda.memoria.bytes_de(agenda.MEMORIA_MAX),
                               extra=agenda.memoria.reserva_pesado()):
        return [], []
    itens = [i["nome"] for i in agenda.fila(base_dados)
             if i.get("prioridade") == 1 and i.get("enfileirada_em") and not agenda._eh_codex(i)
             and agora - conta.de_iso(i["enfileirada_em"]) > ESPERA_MIN]
    return itens, ociosas


def medir(base_dados=None, agora=None, registrar=None):
    """Grava a amostra; a mesma conta ociosa com fila por 15 min abre incidente P1 (uma vez por episódio)."""
    agora = agora or conta.agora()
    itens, ociosas = contar(base_dados, agora)
    est = _ler(_arq(base_dados), {})
    # O episódio precisa pertencer à MESMA conta: intervalos entre tarefas de
    # contas diferentes não se somam em quinze minutos de ociosidade (INC-288).
    anteriores = est.get("desde_por_conta", {})
    desde_por_conta = {n: anteriores.get(n, conta.iso(agora)) for n in ociosas} if itens else {}
    est["desde_por_conta"] = desde_por_conta
    # o episódio é da conta que o abriu: acaba quando nenhuma delas segue ociosa (a conta 3 com fila cheia é outro)
    if est.get("incidente") and not any(n in anteriores for n in desde_por_conta):
        est.pop("incidente", None)
    if itens:
        est["desde"] = min(desde_por_conta.values()) if desde_por_conta else conta.iso(agora)
    else:
        est.pop("desde", None)
        est.pop("incidente", None)
    est["n"], est["itens"], est["ociosas"], est["em"] = len(itens), itens[:10], ociosas, conta.iso(agora)
    if itens and not est.get("incidente") and agora - conta.de_iso(est["desde"]) >= PERSISTE_MIN:
        if registrar is None:
            from hub import incidentes
            registrar = incidentes.registrar
        inc = registrar(f"Conta ociosa ({', '.join(ociosas)}) com {len(itens)} item(ns) p1 esperando há mais de 10 min, "
                        f"desde {est['desde']}: {', '.join(itens[:5])}", "recurso_compartilhado",
                        "hub/agenda.py (coordenar, despachar, vaga extra)", chave="conta-ociosa-com-fila", prioridade=1,
                        base_dados=base_dados, agora=agora)
        est["incidente"] = inc.get("id", "") if isinstance(inc, dict) else ""
    arq = _arq(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(est), encoding="utf-8")
    hist = _arq(base_dados, "ociosidade.jsonl")
    with hist.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"em": est["em"], "n": len(itens), "ociosas": len(ociosas)}) + "\n")
    return est


def resumo(base_dados=None, agora=None, horas=24):
    """{atual, desde, minutos_com_item_24h, amostras}: minutos amostrados com o contador acima de 0 nas últimas `horas`."""
    agora = agora or conta.agora()
    corte = agora - dt.timedelta(hours=horas)
    linhas = []
    try:
        for l in _arq(base_dados, "ociosidade.jsonl").read_text(encoding="utf-8").splitlines():
            linha = _ler_linha(l)
            if linha and conta.de_iso(linha["em"]) >= corte:
                linhas.append(linha)
    except OSError:
        pass
    est = _ler(_arq(base_dados), {})
    return {"atual": est.get("n", 0), "desde": est.get("desde"), "incidente": est.get("incidente", ""),
            "minutos_com_item": sum(1 for l in linhas if l["n"] > 0), "amostras": len(linhas)}


def _ler_linha(l):
    try:
        return json.loads(l)
    except ValueError:
        return None


def podar(base_dados=None, agora=None):
    """Tira do histórico o que passou de 8 dias (chamado de vez em quando pelo `coordenar`)."""
    agora = agora or conta.agora()
    arq = _arq(base_dados, "ociosidade.jsonl")
    try:
        mantidas = [l for l in arq.read_text(encoding="utf-8").splitlines()
                    if (x := _ler_linha(l)) and agora - conta.de_iso(x["em"]) < RETENCAO]
    except OSError:
        return
    arq.write_text("\n".join(mantidas) + ("\n" if mantidas else ""), encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:] == ["resumo"]:
        print(json.dumps(resumo(), ensure_ascii=False))
    else:
        print(json.dumps(medir(), ensure_ascii=False))
