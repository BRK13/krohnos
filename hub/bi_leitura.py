"""Leitura automática do BI: resumo da semana, vigia de queda e gravação da leitura que o painel mostra no topo do /bi.

    python3 -m hub.bi_leitura resumo [--dias 7] [--hoje AAAA-MM-DD]
    python3 -m hub.bi_leitura vigia [--hoje AAAA-MM-DD] [--sem-avisar]     (deploy/bi.sh, todo dia)
    python3 -m hub.bi_leitura gravar < texto.md                           (tarefa bi-semanal)

Só lê o armazém (hub/armazem.py). Dia = data de Brasília; "semana" = N dias terminando ontem.
"""
import argparse
import datetime as dt
import sys
from contextlib import closing

from hub import armazem, conta, ui
from hub.comum.datas import BRT

ORIGEM = "https://nimbus.clientec-com.example"  # endereço público do painel (link no aviso)
LIMITE = 0.20   # variação que ganha ⚠ no resumo
ATRASO_GSC = 3  # os 3 últimos dias do GSC são parciais
# (fonte, métrica, rótulo, estoque?): estoque mostra o último valor; fluxo mostra o total da semana (exceto a CTR: taxa da semana)
METRICAS = [
    ("gsc", "cliques", "Cliques no Google", False), ("gsc", "impressoes", "Aparições no Google", False),
    ("gsc", "ctr", "Taxa de clique no Google", True), ("gsc", "posicao", "Posição média no Google", True),
    ("ga4", "sessoes", "Visitas", False), ("ga4", "sessoes_engajadas", "Visitas engajadas", False),
    ("ga4", "conversoes", "Conversões", False), ("ia", "visitas", "Visitas vindas de IA", False),
    ("stripe", "mrr", "Receita mensal recorrente", True), ("stripe", "assinantes_ativos", "Assinantes ativos", True),
    ("stripe", "novos", "Assinaturas novas", False), ("stripe", "cancelados", "Cancelamentos", False),
    ("stripe", "receita", "Receita recebida", False),
    ("app", "leads", "Contatos novos", False), ("app", "clientes_ativos", "Clientes ativos", True),
    ("app", "agendamentos", "Agendamentos", False), ("app", "faltas", "Faltas", False),
    ("app", "avaliacoes_nota", "Nota das avaliações", True), ("app", "avaliacoes_total", "Total de avaliações", True),
    ("app", "psicologas", "Psicólogas", True), ("app", "fila_espera", "Fila de espera", True),
    ("listmonk", "enviados", "E-mails enviados", False), ("listmonk", "cliques", "Cliques nos e-mails", False),
    ("listmonk", "descadastros", "Descadastros", False), ("listmonk", "rejeicoes", "E-mails rejeitados", False),
    ("prospeccao", "enviados", "Mensagens de prospecção", False), ("prospeccao", "respostas", "Respostas da prospecção", False),
    ("prospeccao", "optouts", "Pedidos para parar", False), ("prospeccao", "demos", "Demonstrações", False),
]
SINAIS = [("receita", "stripe", "mrr"), ("contatos novos", "app", "leads"), ("cliques do Google", "gsc", "cliques")]


def _hoje(hoje):
    return dt.date.fromisoformat(hoje) if isinstance(hoje, str) else (hoje or dt.datetime.now(BRT).date())


def _num(v, casas=0):
    return f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt(metrica, v):
    if metrica == "ctr":
        return _num(v * 100, 1) + "%"
    if metrica in ("mrr", "receita"):
        return "R$ " + _num(v, 2)
    return _num(v, 1 if metrica in ("posicao", "avaliacoes_nota") or abs(v - round(v)) > 0.01 else 0)


def _negocios(base_dados):
    with closing(armazem._ler(base_dados)) as c:
        return [r[0] for r in c.execute("select distinct negocio from fato order by 1")]


def _media(negocio, fonte, metrica, de, ate, base_dados):
    if (fonte, metrica) == ("gsc", "ctr"):  # CTR do período = Σcliques/Σimpressões (fração, como armazenada)
        n = armazem.soma(negocio, fonte, metrica, de.isoformat(), ate.isoformat(), base_dados=base_dados)[1]
        cl = armazem.soma(negocio, fonte, "cliques", de.isoformat(), ate.isoformat(), base_dados=base_dados)[0]
        im = armazem.soma(negocio, fonte, "impressoes", de.isoformat(), ate.isoformat(), base_dados=base_dados)[0]
        return (cl / im, n) if im and n else (None, 0)
    s, n = armazem.soma(negocio, fonte, metrica, de.isoformat(), ate.isoformat(), base_dados=base_dados)
    return (s / n, n) if n else (None, 0)


def _janela(fonte, hoje, dias):
    """(início, fim, início da base, fim da base): a semana termina ontem; a base são os 4×dias antes dela."""
    fim = hoje - dt.timedelta(days=1 + (ATRASO_GSC if fonte == "gsc" else 0))
    ini = fim - dt.timedelta(days=dias - 1)
    return ini, fim, ini - dt.timedelta(days=4 * dias), ini - dt.timedelta(days=1)


def _linha(negocio, fonte, metrica, rotulo, estoque, hoje, dias, bd):
    ini, fim, bini, bfim = _janela(fonte, hoje, dias)
    atual, n = _media(negocio, fonte, metrica, ini, fim, bd)
    if not n:
        return None
    base, _ = _media(negocio, fonte, metrica, bini, bfim, bd)
    if metrica == "ctr":  # taxa ponderada da semana, não a do último dia
        valor = atual
    elif estoque:
        valor = [v for _, v in armazem.serie(negocio, fonte, metrica, ini.isoformat(), fim.isoformat(), base_dados=bd) if v is not None][-1]
    else:
        valor = atual * n
    texto = f"- {rotulo}: {_fmt(metrica, valor)}"
    if not base:  # sem dado, ou média zero (a variação seria infinita)
        return texto + " (sem base)"
    var = atual / base - 1
    nota = (", melhorou" if var < 0 else ", piorou") if metrica == "posicao" and var else ""  # posição menor é melhor
    marca = " ⚠" if abs(var) >= LIMITE else ""
    pct = round(var * 100)
    return f"{texto} ({'+' if pct > 0 else ''}{pct}% contra a média das 4 semanas anteriores{nota}){marca}"


def _origens(negocio, hoje, dias, bd):
    ini, fim, bini, bfim = _janela("ga4", hoje, dias)
    na = armazem.soma(negocio, "ga4", "sessoes", ini.isoformat(), fim.isoformat(), base_dados=bd)[1]
    nb = armazem.soma(negocio, "ga4", "sessoes", bini.isoformat(), bfim.isoformat(), base_dados=bd)[1]
    if not na or not nb:
        return []
    a = dict(armazem.dims(negocio, "ga4", "sessoes", ini.isoformat(), fim.isoformat(), bd))
    b = dict(armazem.dims(negocio, "ga4", "sessoes", bini.isoformat(), bfim.isoformat(), bd))
    # média por dia com dado do total; dia com total mas sem linha da origem vale 0 para ela
    mov = sorted(((o, b.get(o, 0) / nb, a.get(o, 0) / na) for o in a.keys() | b.keys()),
                 key=lambda t: -abs(t[2] - t[1]))
    return [f"  - {o}: {_num(antes, 1)} → {_num(depois, 1)} visitas por dia" for o, antes, depois in mov[:3] if antes != depois]


def resumo(dias=7, hoje=None, base_dados=None):
    hoje = _hoje(hoje)
    ini, fim, _, _ = _janela("ga4", hoje, dias)
    saida = [f"# Resumo de {ini:%d/%m} a {fim:%d/%m} contra a média das 4 semanas anteriores", ""]
    for n in _negocios(base_dados):
        linhas = [x for x in (_linha(n, *m, hoje, dias, base_dados) for m in METRICAS) if x]
        if not linhas:
            continue
        saida += [f"## {n}", *linhas]
        if any(m[0] == "gsc" and _linha(n, *m, hoje, dias, base_dados) for m in METRICAS):
            saida.append(f"- (Google até {_janela('gsc', hoje, dias)[1]:%d/%m}: os últimos dias ainda não fecharam)")
        origens = _origens(n, hoje, dias, base_dados)
        if origens:
            saida += ["- Maiores mudanças de origem das visitas:", *origens]
        saida.append("")
    return "\n".join(saida).rstrip() + "\n"


def quedas(hoje=None, base_dados=None):
    """Sinais cujo dia avaliado caiu mais de 40% contra o mesmo dia da semana nas 4 semanas anteriores."""
    hoje = _hoje(hoje)
    achados = []
    for n in _negocios(base_dados):
        for nome, fonte, metrica in SINAIS:
            dia = hoje - dt.timedelta(days=1 + (ATRASO_GSC if fonte == "gsc" else 0))
            s = dict(armazem.serie(n, fonte, metrica, (dia - dt.timedelta(days=28)).isoformat(), dia.isoformat(), base_dados=base_dados))
            valor = s[dia.isoformat()]
            base = [v for k in (7, 14, 21, 28) if (v := s[(dia - dt.timedelta(days=k)).isoformat()]) is not None]
            if valor is None or len(base) < 2:
                continue
            media = sum(base) / len(base)
            if (media > 0 if metrica == "mrr" else media >= 5) and valor <= 0.6 * media:
                achados.append({"negocio": n, "sinal": nome, "metrica": metrica, "dia": dia.isoformat(),
                                "valor": valor, "media": media, "queda": 1 - valor / media})
    return achados


def _mensagem(achados):
    linhas = [f"Queda forte no BI ({ui.NOME_PRODUTO}):"]
    for q in achados:
        d = dt.date.fromisoformat(q["dia"])
        linhas.append(f"- {q['negocio']}, {q['sinal']}: {_fmt(q['metrica'], q['valor'])} em {d:%d/%m}, média do mesmo dia da "
                      f"semana {_fmt(q['metrica'], q['media'])} (queda de {_num(q['queda'] * 100)}%) {ORIGEM}/bi/{q['negocio']}")
    return "\n".join(linhas)


def vigiar(hoje=None, base_dados=None, avisar=conta.avisar):
    """Uma mensagem por dia no máximo; devolve o texto enviado, ou None se não houve queda ou já avisou hoje."""
    hoje = _hoje(hoje)
    achados = quedas(hoje, base_dados)
    if not achados:
        print("sem queda grave")
        return None
    marca = armazem.caminho(base_dados).parent / "vigia-ultimo.txt"
    if marca.exists() and marca.read_text().strip() == hoje.isoformat():
        print("já avisou hoje")
        return None
    texto = _mensagem(achados)
    avisar(texto)
    marca.write_text(hoje.isoformat())
    return texto


def gravar(texto, base_dados=None):
    """Grava no próprio arquivo (o usuário claude só tem escrita nele, sem criar arquivo ao lado)."""
    if not texto.strip():
        raise ValueError("texto vazio: nada gravado")
    with open(armazem.caminho(base_dados).parent / "leitura-semana.md", "w", encoding="utf-8") as f:
        f.write(texto)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hub.bi_leitura")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resumo")
    r.add_argument("--dias", type=int, default=7)
    r.add_argument("--hoje")
    v = sub.add_parser("vigia")
    v.add_argument("--hoje")
    v.add_argument("--sem-avisar", action="store_true")
    sub.add_parser("gravar")
    a = ap.parse_args(argv)
    if a.cmd == "resumo":
        print(resumo(a.dias, a.hoje), end="")
    elif a.cmd == "vigia":
        if a.sem_avisar:
            q = quedas(a.hoje)
            print(_mensagem(q) if q else "sem queda grave")
        else:
            texto = vigiar(a.hoje)
            if texto:
                print(texto)
    else:
        try:
            gravar(sys.stdin.read())
        except ValueError as e:
            sys.exit(str(e))


if __name__ == "__main__":
    main()
