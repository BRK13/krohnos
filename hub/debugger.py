"""Faixa de conserto reativa. Estado persistido, uma tarefa por repo, sem WhatsApp."""
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from hub import config, conta, consertos, data, esperas, incidentes, tarefas


RE_PR_URL = re.compile(r'github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)')
RE_PR_NUM = re.compile(r'(?:\bPRs?\s*#?|(?<![\w/&])#)(\d+)', re.I)
RE_INC = re.compile(r'\bINC-\d+\b')
SEM_CODIGO = re.compile(r'sem mudan[çc]a de c[óo]digo', re.I)
PEDIDO_POS_MERGE = {'repo-privado': 'atualizar-hub', 'nimbus-hub': 'atualizar-hub', 'bot_whatsapp': 'atualizar-runner'}


def arquivo(base=None):
    return conta.pasta(base) / 'debugger.json'


def ler(base=None):
    try:
        estado = json.loads(arquivo(base).read_text())
        if not isinstance(estado, dict) or not isinstance(estado.get('itens'), dict):
            return {'itens': {}}
        return estado
    except (OSError, ValueError):
        return {'itens': {}}


def salvar(estado, base=None):
    from hub.alertas import salvar_estado
    salvar_estado(arquivo(base), estado)


def chave(i):
    return hashlib.sha256(f"{i['repo']}:{i['alvo']}".encode()).hexdigest()[:20]


def branch_da_classe(classe):
    return f'codex/debugger-{re.sub("[^a-z0-9]+", "-", classe.lower()).strip("-")}'


def pr_da_classe(pulls, classe):
    """PR aberto do Debugger para a classe: pela branch `codex/debugger-<classe>` ou pelo título `fix: INC-…` que cita a classe."""
    branch = branch_da_classe(classe)
    for p in pulls:
        titulo = p.get('title') or ''
        if p['head']['ref'] == branch or (titulo.lower().startswith('fix: inc') and classe in f"{titulo} {p.get('body') or ''}"):
            return p
    return None


def _item(repo, alvo, erro, desde, **extra):
    return dict(repo=repo, alvo=alvo, erro=erro, desde=desde, **extra)


def coletar_repo(repo, gh, agora, base):
    itens = []
    branch = gh(f'repos/{repo}')['default_branch']
    pulls = gh(f'repos/{repo}/pulls?state=open&per_page=100')
    abertos = {f"pr:{p['number']}" for p in pulls}
    for i in ler(base)['itens'].values():
        if i['repo'] == repo and i['alvo'].startswith('pr:') and i['alvo'] not in abertos and not i.get('resolvido_em'):
            pr = gh(f"repos/{repo}/pulls/{i['alvo'].split(':')[1]}")
            status = gh(f"repos/{repo}/commits/{pr['head']['sha']}/statuses?per_page=100") or []
            ci = next((s for s in status if s.get('context') == 'local-ci'), {})
            if ci.get('state') != 'success':
                fechado = {'encerrado_em': conta.iso(agora)} if pr.get('state') == 'closed' else {}
                itens.append({**i, **fechado, 'pronto': False, 'aguardando_ci': True})
    for p in [None, *pulls]:
        ref = p['head']['sha'] if p else branch
        status = gh(f'repos/{repo}/commits/{ref}/statuses?per_page=100') or []
        rodadas = [s for s in status if s.get('context') == 'local-ci']
        if not rodadas or rodadas[0]['state'] not in ('failure', 'error'):
            if not rodadas or rodadas[0]['state'] != 'success':
                alvo = f"pr:{p['number']}" if p else branch
                itens += [{**i, "pronto": False, "aguardando_ci": True} for i in ler(base)['itens'].values() if i['repo'] == repo and i['alvo'] == alvo and not i.get('resolvido_em')]
            continue
        s = rodadas[0]
        erro = re.sub(r'\s*\([^)]*\)\s*$', '', s.get('description') or 'local-ci vermelho')
        desde = s.get('created_at') or s['updated_at']
        commit = gh(f'repos/{repo}/commits/{ref}')
        sha = commit.get('sha', ref)
        novo = commit['commit']['committer']['date']
        repetido = sum(r.get('state') in ('failure', 'error') and re.sub(r'\s*\([^)]*\)\s*$', '', r.get('description') or '') == erro for r in rodadas[:2]) >= 2
        m = re.search(r'KRN-\d+', f"{(p or {}).get('body') or ''} {(p or {}).get('head', {}).get('ref', '')}", re.I)
        itens.append(_item(repo, f"pr:{p['number']}" if p else branch, erro, desde,
                           tipo='ci', aguardando_ci=False, sha=sha, rodada=f'{sha}:{desde}', branch=p['head']['ref'] if p else branch,
                           url=p['html_url'] if p else f'https://github.com/{repo}',
                           issue=m.group().upper() if m else '',
                           pronto=not p or repetido or agora - conta.de_iso(max(desde, novo)) >= dt.timedelta(minutes=30)))
    return itens


def _data_valida(valor):
    try:
        return conta.de_iso(valor).tzinfo is not None
    except (ValueError, TypeError, AttributeError):
        return False


def _conservar(p, itens, base):
    itens.extend(i for i in ler(base)['itens'].values()
                 if i.get('fonte') == str(p) and not i.get('resolvido_em'))


def _fonte(p, itens, base):
    try:
        a = json.loads(p.read_text())
        if not isinstance(a, dict):
            raise ValueError('objeto esperado')
        return a
    except (OSError, ValueError):
        _conservar(p, itens, base)
        return {}


def coletar(base=None, gh=None, agora=None, repos=None):
    """Falha de fonte conserva o estado anterior: nunca resolve vermelho por erro de rede."""
    gh, agora = gh or esperas.gh_api, agora or conta.agora()
    base = Path(base or data())
    itens = []
    from hub import sites
    ativos = {config.repo(r) for r in consertos.REPOS_ATIVOS} | {(s.get('repo') or {}).get('github') for s in sites.ativos()}
    for nome in repos if repos is not None else sorted(r for r in ativos if r):
        repo = nome if '/' in nome else config.repo(nome)
        try:
            itens += coletar_repo(repo, gh, agora, base)
        except Exception:  # fonte fora: mantém o vermelho anterior, nunca inventa verde
            itens += [i for i in ler(base)['itens'].values() if i['repo'] == repo and i['tipo'] == 'ci' and not i.get('resolvido_em')]
    for p in (conta.pasta(base) / 'main-vermelho').glob('*.json'):
        a = _fonte(p, itens, base)
        if not a:
            _conservar(p, itens, base)
            continue
        if not all(a.get(k) for k in ('teste', 'desde', 'sha', 'issue')) or not _data_valida(a.get('desde')):
            _conservar(p, itens, base)
            continue
        repo = p.stem.replace('__', '/')
        if not any(i['repo'] == repo and not i['alvo'].startswith('pr:') for i in itens):
            itens.append(_item(repo, 'main', a['teste'], a['desde'], tipo='ci', fonte=str(p), sha=a['sha'],
                               issue=a['issue'], branch='main', pronto=True))
        else:
            for i in itens:
                if i['repo'] == repo and not i['alvo'].startswith('pr:'):
                    i['issue'] = a['issue']
    for pasta, repo in ((base / 'alertas' / 'fsite05', config.repo("cliente-a-v2")),
                        (base / 'vigia-clientej', config.repo("clienteh"))):
        for p in pasta.glob('*.json'):
            a = _fonte(p, itens, base)
            if not a:
                _conservar(p, itens, base)
                continue
            if a.get('tratado') or a.get('estado') in ('ok', 'info'):
                continue
            if not a.get('estado') or not (a.get('mensagem') or a.get('rotulo')) or not _data_valida(a.get('desde') or a.get('criado_em')):
                _conservar(p, itens, base)
                continue
            itens.append(_item(repo, str(p), a.get('mensagem') or a.get('rotulo') or p.stem,
                               a.get('desde') or a['criado_em'], tipo='alerta', fonte=str(p)))
    p = base / 'alertas' / 'estado.json'
    alertas = _fonte(p, itens, base)
    registros = alertas.get('itens', {})
    if not isinstance(registros, dict):
        _conservar(p, itens, base)
        registros = {}
    for k, a in registros.items():
        if not isinstance(a, dict) or not a.get('texto') or not _data_valida(a.get('desde')):
            _conservar(p, itens, base)
            continue
        if a.get('tratado') or a.get('tipo') not in ('site', 'deploy'):
            continue
        from hub import sites
        site = next((s for s in sites.ativos() if a.get('item') in (s.get('dominio'), s.get('id'))), {})
        repo = (site.get('repo') or {}).get('github') or config.repo("repo-privado")
        itens.append(_item(repo, k, a['texto'], a['desde'], tipo='alerta', fonte=str(base / 'alertas' / 'estado.json')))
    hub = config.repo("repo-privado")
    pulls_hub = None
    for a in incidentes.listar(so_pendentes=True, base_dados=base):
        if a.get('classe') == 'main_vermelho':
            continue  # o CI já é a fonte; não duplicar a tarefa
        extra = {}
        if a.get('classe'):  # um PR por classe; o PR aberto da classe recebe o conserto e segura o disparo
            extra = {'classe': a['classe'], 'branch_conserto': branch_da_classe(a['classe'])}
            try:
                if pulls_hub is None:
                    pulls_hub = gh(f'repos/{hub}/pulls?state=open&per_page=100')
                pr = pr_da_classe(pulls_hub, a['classe'])
            except Exception:  # noqa: BLE001 — sem rede: segue sem saber do PR
                pr = None
            if pr:
                extra.update(pr_aberto=pr['html_url'], branch_conserto=pr['head']['ref'])
        itens.append(_item(hub, a['id'], a['falha'], a['em'], tipo='alerta', issue=a.get('issue', ''), **extra))
    itens += _falhas_do_bot(agora)
    return itens


def _um_pr_por_classe(i):
    if not i.get('classe'):
        return ''
    if i.get('pr_aberto'):
        return (f"UM PR POR CLASSE ({i['classe']}): já existe o PR aberto {i['pr_aberto']} (branch {i['branch_conserto']}). "
                f"Some o conserto deste incidente NELE (checkout da branch remota), cite {i['alvo']} no título "
                f"(`fix: INC-a, INC-b: {i['classe']}`) e no corpo. Não abra outro PR.")
    return (f"UM PR POR CLASSE ({i['classe']}): use a branch {i['branch_conserto']} e abra o PR `fix: {i['alvo']}: {i['classe']}` "
            f"com {i['classe']} no corpo; os próximos incidentes da classe somam o conserto nele.")


def _falhas_do_bot(agora, arq=None):
    """`falha` do notas-tecnicas.jsonl do bot (últimas 24 h): evento, não estado; sai da foto ao envelhecer ou
    ao ser tratada (hub.debugger tratar). Nota comum não entra aqui: vai ao resumo (consertos.blocos_resumo)."""
    falhas = consertos.notas_bot(agora - dt.timedelta(hours=24), agora, arq)[0]
    fonte = arq or os.environ.get('NOTAS_TECNICAS_ARQ') or consertos.NOTAS_TECNICAS
    return [_item(config.repo('bot_whatsapp'), 'bot-falha:' + hashlib.sha256(f"{r['em']}{r['onde']}".encode()).hexdigest()[:12],
                  f"falha do bot ({r['onde']}): {' '.join(r['texto'].split())[:300]}", r['em'], tipo='alerta',
                  fonte=fonte) for r in falhas if _data_valida(r['em'])]


def tarefa(i):
    nome = 'debugger-' + re.sub('[^a-z0-9]+', '-', i['repo'].lower()).strip('-')
    prompt = f"""Debugger: tentativa {i['tentativas']}/3. Repo {i['repo']}; alvo {i['alvo']}.
Erro: {i['erro']}. Desde {i['desde']}. Issue dona: {i.get('issue') or 'descubra pelo PR; comente nela'}.
URL: {i.get('url', '')}. Fonte do alerta: {i.get('fonte', '')}. Chave: {chave(i)}.
Use worktree própria; nunca edite a worktree da issue dona. PR: conserte na MESMA branch remota {i.get('branch', '')}.
Main/master/develop: crie branch codex/ e PR de conserto contra {i.get('branch', '')}.
Leia AGENTS.md e CLAUDE.md. Leia o log abaixo antes de alterar código; não rode build nem ci-local à mão.
Rode testes focados. Faça commit, push e PR quando necessário. O resultado final será comentado na issue dona.
Alerta: investigue e só após tratar execute python3 -m hub.debugger tratar {chave(i)} --nota "causa, conserto e prova". Não altere arquivos de outro usuário. Incidente: hub.linear incidente feito.
{_um_pr_por_classe(i)}
Tratar e `incidente feito` RECUSAM nota que cita PR aberto (KRN-1050): o conserto só conta com o PR mergeado na main e o
atualizar-hub rc=0 (o hub faz o merge e o pedido quando o local-ci e a revisão passam; a nota leva "atualizar-hub rc=0"),
ou com a nota "sem mudança de código". Com o PR aberto, termine sem marcar nada: o hub segue o PR.
`incidente feito` também exige o teste de regressão citado na nota (tests/test_x.py; KRN-1095): o PR do conserto leva o teste.
Registre causa e conserto em hub.consertos registrar --por debugger. Nunca WhatsApp por alerta técnico:
regra feedback_alerta_tecnico_orquestrador. Ao esgotar tentativas, precisa_fulano só no resumo.
Não espere CI na sessão: deixe o runner trabalhar e termine; o Debugger confere no próximo tick.
Log do local-ci:\n{esperas.trecho_log(i.get('sha', '')) if i.get('sha') else ''}
"""
    t = tarefas.Tarefa(nome, 'manual', 1, 'opus', 1800, [i['repo']], False, 'Debugger: consertar vermelho/alerta', prompt)
    t.debugger = i
    return t


def vigiar(base=None, agora=None, coletar_fn=None):
    from hub import agenda
    agora = agora or conta.agora()
    arquivo(base).parent.mkdir(parents=True, exist_ok=True)
    # mesma trava entre ticks/contas; enfileirar e estado são uma única seção crítica
    with conta.trava(caminho=str(arquivo(base)) + '.lock'):
        estado = ler(base)
        # Migra chaves antigas sem perder o orçamento de uma sequência vermelha.
        migrados = {}
        for anterior in estado['itens'].values():
            k = chave(anterior)
            if k not in migrados or not anterior.get('resolvido_em'):
                existente = migrados.get(k, {})
                anterior['tentativas'] = anterior.get('tentativas', 0) + existente.get('tentativas', 0)
                migrados[k] = anterior
        estado['itens'] = migrados
        foto = (coletar_fn or coletar)(base=base, agora=agora)
        atuais = {chave(i): i for i in foto if not (estado['itens'].get(chave(i), {}).get('tratado') and
                  estado['itens'][chave(i)]['desde'] == i['desde'])}
        ocupados = {r['nome'] for r in agenda.rodando_todas(base).values()} | {i['nome'] for i in agenda.fila(base)}
        for k, i in estado['itens'].items():
            if k not in atuais and any(a['repo'] == i['repo'] and a['alvo'] == i['alvo'] for a in atuais.values()):
                i['substituido_em'] = conta.iso(agora)
                continue
            if k not in atuais and not i.get('resolvido_em') and not i.get('substituido_em'):
                i['resolvido_em'] = (i.get('tratado') or {}).get('em') or conta.iso(agora)
                consertos.registrar(i['erro'], 'fonte voltou a verde/tratado', 'confirmado pelo Debugger',
                                    'consertado', por='debugger', base_dados=base, agora=agora)
        for k, atual in atuais.items():
            i = estado['itens'].setdefault(k, {**atual, 'tentativas': 0})
            if i.get('resolvido_em'):
                i = estado['itens'][k] = {**atual, 'tentativas': 0}
            if atual.get('rodada') and atual['rodada'] != i.get('ultima_rodada'):
                i['rodadas'] = i.get('rodadas', 0) + 1
                i['ultima_rodada'] = atual['rodada']
            i.pop('substituido_em', None)
            if not atual.get('encerrado_em'):
                i.pop('encerrado_em', None)
            i.update({x: v for x, v in atual.items() if x != 'desde'})
            if i.get('encerrado_em'):
                continue
            t = tarefa(i)
            if t.nome in ocupados:
                continue
            if i.get('ultima_em') and agora - conta.de_iso(i['ultima_em']) < dt.timedelta(minutes=10):
                continue
            if i['tentativas'] >= 3:
                if not i.get('escalado_em'):
                    consertos.registrar(i['erro'], '3 tentativas sem resolver', 'precisa de intervenção',
                                        'precisa_fulano', por='debugger', base_dados=base, agora=agora)
                    i['escalado_em'] = conta.iso(agora)
                continue
            pronto = i.get('rodadas', 0) >= 2 or i.get('pronto', agora - conta.de_iso(i['desde']) >= dt.timedelta(minutes=30))
            if not pronto or i.get('aguardando_ci'):
                continue
            if i.get('pr_aberto') and i.get('tentativas'):  # o PR da classe já tem tentativa: o hub segue o PR (CI, revisão, merge)
                continue
            i['tentativas'] += 1
            t = tarefa(i)
            if agenda.enfileirar(t, 'Debugger reativo', base):
                i['ultima_em'] = conta.iso(agora)
                ocupados.add(t.nome)
                consertos.registrar(i['erro'], 'gatilho reativo', f"tentativa {i['tentativas']}/3", 'tentando',
                                    por='debugger', base_dados=base, agora=agora)
            else:
                i['tentativas'] -= 1
        salvar(estado, base)


def painel(base=None, agora=None):
    agora = agora or conta.agora()
    itens = list(ler(base)['itens'].values())
    abertos = [i for i in itens if not i.get('resolvido_em') and not i.get('substituido_em') and not i.get('encerrado_em')]
    resolvidos = [i for i in itens if i.get('resolvido_em') and agora - conta.de_iso(i['resolvido_em']) <= dt.timedelta(hours=24)]
    def media(tipo):
        tempos = [(conta.de_iso(i['resolvido_em']) - conta.de_iso(i['desde'])).total_seconds() / 60
                  for i in resolvidos if i['tipo'] == tipo]
        return round(sum(tempos) / len(tempos), 1) if tempos else None
    return {'abertos': abertos, 'ci_aberto': sum(i['tipo'] == 'ci' for i in abertos),
            'ci_resolvido_24h': sum(i['tipo'] == 'ci' for i in resolvidos),
            'ci_media_min': media('ci'), 'alerta_media_min': media('alerta')}


def resultado(i, texto, base=None):
    """Entrega do executor na issue dona; verde/tratado só será confirmado pela fonte."""
    from hub import linear
    consertos.registrar(i['erro'], 'execução do Debugger', texto or 'sem resposta do executor',
                        'tentando', por='debugger', prova=i.get('url', ''), base_dados=base)
    if i.get('issue'):
        linear.comentar(i['issue'], f"Debugger · tentativa {i['tentativas']}/3\n\n{texto[:12000]}")


def alerta_tecnico(texto):
    """feedback_alerta_tecnico_orquestrador: diário/resumo, jamais WhatsApp."""
    consertos.registrar(texto, 'alerta técnico encaminhado ao orquestrador',
                        'acompanhar no Debugger e no resumo', 'tentando', por='debugger')


def prs_citados(nota, repo):
    """(repo, número) dos PRs que a nota cita: URL completa, ou `#N`/`PR N` (do `repo` dado)."""
    achados = {(r, int(n)) for r, n in RE_PR_URL.findall(nota)}
    sem_url = RE_PR_URL.sub(' ', nota)
    achados |= {(repo, int(n)) for n in RE_PR_NUM.findall(sem_url)}
    return sorted(achados)


def exigir_pr_fechado(nota, repo, gh=None):
    """conserto só conta com o PR na main. Nota que cita PR aberto, ou mergeado sem o `atualizar-hub` com
    rc=0 dito na nota, é recusada (ValueError). "sem mudança de código" na nota, ou nenhum PR citado, passa."""
    gh = gh or esperas.gh_api
    for r, n in prs_citados(nota, repo):
        try:
            pr = gh(f'repos/{r}/pulls/{n}')
        except subprocess.CalledProcessError as e:
            if '404' in (e.stderr or '') or 'Not Found' in (e.stderr or ''):
                continue  # `#N` era issue, não PR
            raise ValueError(f'não consegui conferir o PR {r}#{n}; não trato com PR no caminho') from e
        except Exception as e:  # noqa: BLE001
            raise ValueError(f'não consegui conferir o PR {r}#{n}; não trato com PR no caminho') from e
        if pr.get('merged') or pr.get('merged_at'):
            pedido = PEDIDO_POS_MERGE.get(r.split('/')[-1])
            if pedido and not re.search(rf'{pedido}\W+(?:\w+\W+){{0,3}}rc\s*=\s*0', nota):
                raise ValueError(f'PR {r}#{n} mergeado, mas a nota não traz `{pedido}` com rc=0')
        elif pr.get('state') != 'open':
            raise ValueError(f'PR {r}#{n} fechado sem merge: conserto abandonado não conta; reabra ou use a nota "sem mudança de código"')
        else:
            raise ValueError(f'PR {r}#{n} ainda aberto: alerta só fica tratado com o PR mergeado na main e '
                             f'o {PEDIDO_POS_MERGE.get(r.split("/")[-1], "deploy")} rc=0, ou com a nota "sem mudança de código"')


def tratar(id_, nota, base=None, agora=None, gh=None):
    """Recibo do tratamento sem regravar fontes mantidas pelo root/orquestrador."""
    with conta.trava(caminho=str(arquivo(base)) + '.lock'):
        estado = ler(base)
        i = estado['itens'][id_]
        if i['tipo'] != 'alerta' or not nota.strip():
            raise ValueError('exige alerta e nota do tratamento')
        if not SEM_CODIGO.search(nota):
            exigir_pr_fechado(nota, i['repo'], gh)
        i['tratado'] = {'em': conta.iso(agora or conta.agora()), 'por': 'debugger', 'o_que': nota.strip()}
        salvar(estado, base)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('acao', choices=['tratar'])
    ap.add_argument('chave')
    ap.add_argument('--nota', required=True)
    args = ap.parse_args()
    tratar(args.chave, args.nota)
