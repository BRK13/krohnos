"""Incidentes do harness (princípio 14 da receita Lunar): toda falha registra a classe e a peça do harness
que muda. A retro diária (tarefas/linear-retro.md) lista os do dia e os pendentes.

    python3 -m hub.linear incidente registrar --falha "..." --classe pronto_sem_recibo --peca "hub/recibo.py" [--issue]
    python3 -m hub.linear incidente feito INC-3 --nota "PR #240; teste tests/test_recibo.py" (teste citado)
    python3 -m hub.linear incidente listar [--horas 24] [--pendentes]
"""
import datetime as dt
import json
import re

from hub import conta

# As 7 primeiras são da; o resto vem da mineração das ~1.000 tarefas (docs/licoes/classes.json: peça por classe).
CLASSES = ("pronto_sem_recibo", "tarefa_parada", "espera_ci_na_sessao", "regra_so_no_prompt", "main_vermelho",
           "teste_em_dado_real", "outra",
           "laco_de_execucoes", "repetiu_decisao_do_fulano", "cota_estourada", "entrega_visual_sem_conferir",
           "deploy_nao_subiu_comentado", "executou_sem_pedir", "recurso_compartilhado", "espera_aprovacao_desnecessaria",
           "teste_que_nao_prova", "revisao_nunca_chega", "armadilha_do_ambiente", "spam_whatsapp", "bot_erra_com_terceiro",
           "entrega_incompleta_ou_fora_do_canal", "numero_inventado", "copy_fora_do_padrao", "pede_trabalho_ao_fulano",
           "deploy_nao_verificado", "acao_duplicada", "frentes_em_paralelo", "reconstruiu_pronto", "hub_quebrado_em_producao",
           "bug_de_logica", "perda_de_dado_ou_segredo", "duplicacao_ou_corrida", "frente_dada_por_pronta",
           "aviso_falso_ao_dono")  # o alarme da confiabilidade (hub/confiabilidade.py) abre como P1
ESTADOS = ("pendente", "feito")
# (F2): fechar exige o teste de regressão citado na nota (tests/test_x.py, tests.test_x ou test_x.py). Isento só o
# que não teve conserto: "descartado: ..." (falso positivo, duplicata), "sem mudança de código" ou "sem teste: <motivo>".
RE_TESTE = re.compile(r"(?:^|[\s/`'\"(])tests?/test_[\w.-]+\.py|\btests\.test_\w+|\btest_[\w-]+\.py")
RE_ISENTO = re.compile(r"^\s*descartad[oa]:|sem mudan[çc]a de c[óo]digo|(?:^|[;.]\s*)sem teste:\s*\S.{8,}", re.I)


def cita_teste(nota):
    return bool(RE_TESTE.search(nota or ""))


def isento_de_teste(nota):
    return not cita_teste(nota) and bool(RE_ISENTO.search(nota or ""))


def _arq(base_dados=None):
    return conta.pasta(base_dados) / "incidentes.jsonl"


def _trava(base_dados=None):
    # registrar/marcar leem o arquivo antes de gravar (id INC-N, chave): sem trava, dois processos pegam o mesmo id
    _arq(base_dados).parent.mkdir(parents=True, exist_ok=True)
    return conta.trava(caminho=str(_arq(base_dados)) + ".lock")


def _linhas(base_dados=None):
    try:
        texto = _arq(base_dados).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for l in texto.splitlines():
        try:
            r = json.loads(l)
        except ValueError:  # linha cortada por queda no meio da escrita: não derruba a retro
            continue
        if isinstance(r, dict) and "id" in r:
            out.append(r)
    return out


def _gravar(reg, base_dados=None):
    arq = _arq(base_dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    # linha cortada sem \n (queda no meio da escrita) não pode engolir o registro seguinte
    cortada = arq.exists() and arq.stat().st_size and not arq.read_bytes().endswith(b"\n")
    with open(arq, "a", encoding="utf-8") as f:
        f.write(("\n" if cortada else "") + json.dumps(reg, ensure_ascii=False) + "\n")


def listar(horas=None, so_pendentes=False, base_dados=None, agora=None):
    """Último estado de cada incidente (o registro inicial + as marcações), do mais velho ao mais novo."""
    agora = agora or conta.agora()
    por_id = {}
    for r in _linhas(base_dados):  # a marcação ("feito") sobrepõe o registro e mantém os campos dele
        por_id[r["id"]] = {**por_id.get(r["id"], {}), **r}
    itens = list(por_id.values())
    if horas is not None:
        corte = agora - dt.timedelta(hours=horas)
        itens = [i for i in itens if conta.de_iso(i["em"]) >= corte or i["estado"] == "pendente"]
    if so_pendentes:
        itens = [i for i in itens if i["estado"] == "pendente"]
    return itens


def registrar(falha, classe, peca, issue="", estado="pendente", chave="", base_dados=None, agora=None, prioridade=2):
    if classe not in CLASSES:
        raise ValueError(f"classe tem que ser uma de {', '.join(CLASSES)}")
    if estado not in ESTADOS:
        raise ValueError(f"estado tem que ser um de {', '.join(ESTADOS)}")
    if not falha.strip() or not peca.strip():
        raise ValueError("falta a falha ou a peça do harness que muda")
    with _trava(base_dados):
        if chave:  # a mesma falha repetida no tick seguinte não vira outro incidente
            for i in listar(so_pendentes=True, base_dados=base_dados, agora=agora):
                if i.get("chave") == chave:
                    return i
        reg = {"id": f"INC-{len({r['id'] for r in _linhas(base_dados)}) + 1}",
               "em": conta.iso(agora or conta.agora()), "falha": falha.strip()[:500], "classe": classe,
               "peca": peca.strip()[:300], "issue": issue.strip()[:20], "estado": estado, "chave": chave[:120],
               "prioridade": prioridade}
        _gravar(reg, base_dados)
        return reg


def marcar(id_, estado="feito", nota="", base_dados=None, agora=None, gh=None):
    if estado not in ESTADOS:
        raise ValueError(f"estado tem que ser um de {', '.join(ESTADOS)}")
    if estado == "feito" and not (cita_teste(nota) or isento_de_teste(nota)):
        raise ValueError("incidente feito exige o teste de regressão citado na nota (tests/test_x.py); sem conserto: "
                         "'descartado: <por quê>', 'sem mudança de código' ou 'sem teste: <motivo>'")
    if estado == "feito":  # nunca [feito] com o PR do conserto ainda aberto
        from hub import config, debugger
        debugger.exigir_pr_fechado(nota, config.repo("repo-privado"), gh)
    with _trava(base_dados):
        if id_ not in {r["id"] for r in _linhas(base_dados)}:
            raise ValueError(f"incidente {id_} não existe")
        reg = {"id": id_, "estado": estado, "nota": nota.strip()[:300],
               "marcado_em": conta.iso(agora or conta.agora())}
        _gravar(reg, base_dados)
        return reg


def texto_retro(horas=24, base_dados=None, agora=None):
    itens = listar(horas, base_dados=base_dados, agora=agora)
    if not itens:
        return "## Incidentes do harness\n- nenhum"
    linhas = ["## Incidentes do harness (do dia e os pendentes)"]
    for i in itens:
        linhas.append(f"- {i['id']} [{i['estado']}]{' P1' if i.get('prioridade') == 1 else ''} {i['classe']}{' · ' + i['issue'] if i.get('issue') else ''}: "
                      f"{i['falha']} → muda: {i['peca']}{' (' + i['nota'] + ')' if i.get('nota') else ''}")
    return "\n".join(linhas)
