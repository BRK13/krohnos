"""Auditoria somente leitura e atribuição conservadora de projetos."""
from collections import defaultdict, deque
from datetime import date
from fnmatch import fnmatchcase
import re
from hub import grafo as G

ARQUITETURA = {'projeto', 'pagina', 'repo', 'app', 'dominio', 'vps', 'banco', 'servico', 'cron', 'segredo'}


def projetos(ix):
    """Menor caminho estrutural; VPS compartilhada e texto não propagam dono."""
    donos, adj = {}, defaultdict(set)
    for de, rs in ix.sai.items():
        for rel, para in rs:
            if rel != 'menciona' and all(ix.ent[x]['tipo'] in ARQUITETURA - {'vps'} for x in (de, para)):
                adj[de].add(para); adj[para].add(de)
    # Command/path evidence is local to the entity. Never infer from its VPS.
    aliases = {
        'clientej': r'clienteh|clientej', 'asite01': r'asite01(?:-wpp|-control)?',
        'fsite05': r'fsite05(?:-clientem(?:-v2)?)?', 'clientei': r'clientei(?:psi-v2)?',
        'clientee': r'clientee(?:-moraes-site)?', 'psite13': r'psite13',
        'evento_exemplo': r'evento_exemplo(?:-fulano-clientei)?',
        'krohnos': r'nimbus(?:-hub)?|krohnos',
        'clienten': r'clienten', 'radar': r'radar(?:-infraestrutura|-infra)?',
    }
    evidencias = {}
    for eid, n in ix.ent.items():
        if n['tipo'] not in ARQUITETURA - {'projeto', 'vps'}:
            continue
        # Secret names are generic: only their source path is evidence.
        texto = n.get('caminho', '') if n['tipo']=='segredo' else ' '.join(
            str(n.get(k, '')) for k in ('id', 'nome', 'caminho', 'desc'))
        matches = {f'projeto:{key}' for key, pattern in aliases.items()
                   if f'projeto:{key}' in ix.ent and re.search(
                       r'(?<![a-z0-9])(?:' + pattern + r')(?![a-z0-9])', re.sub(r'cliente-q-clientei', 'evento_exemplo', texto.lower()))}
        if len(matches) == 1:
            evidencias[eid] = matches.pop()
    candidatos_por = defaultdict(set)
    distancias = {}
    fila = deque()
    for eid in sorted(ix.ent):
        if ix.ent[eid]['tipo'] == 'projeto':
            candidatos_por[eid].add(eid); distancias[eid] = 0; fila.append(eid)
    while fila:
        eid = fila.popleft()
        for outro in sorted(adj[eid]):
            distancia = distancias[eid] + 1
            if outro not in distancias or distancia < distancias[outro]:
                distancias[outro] = distancia
                candidatos_por[outro] = set(candidatos_por[eid]); fila.append(outro)
            elif distancia == distancias[outro]:
                novos = candidatos_por[eid] - candidatos_por[outro]
                if novos:
                    candidatos_por[outro].update(novos); fila.append(outro)
    donos = {eid: next(iter(ids)) for eid, ids in candidatos_por.items() if len(ids) == 1}
    # A second multi-source search only covers components without a structural
    # project. Equal-distance evidence stays ambiguous, regardless of input order.
    inferidos = {eid: {projeto} for eid, projeto in evidencias.items() if eid not in candidatos_por}
    dist = {eid: 0 for eid in inferidos}
    fila = deque(sorted(inferidos))
    while fila:
        eid = fila.popleft()
        for outro in sorted(adj[eid]):
            if outro in candidatos_por:
                continue
            distancia = dist[eid] + 1
            if outro not in dist or distancia < dist[outro]:
                dist[outro] = distancia
                inferidos[outro] = set(inferidos[eid]); fila.append(outro)
            elif distancia == dist[outro]:
                novos = inferidos[eid] - inferidos[outro]
                if novos:
                    inferidos[outro].update(novos); fila.append(outro)
    donos.update({eid: next(iter(ids)) for eid, ids in inferidos.items() if len(ids) == 1})
    for eid in ix.ent:
        if eid not in donos and ix.ent[eid]['tipo'] not in ARQUITETURA:
            candidatos = {donos[x] for _, x in ix.sai.get(eid, []) + ix.entra.get(eid, []) if x in donos and ix.ent[x]['tipo'] in ARQUITETURA - {'vps'}}
            if len(candidatos) == 1:
                donos[eid] = candidatos.pop()
    return donos


def excecoes(ix, regras=None):
    """{id: motivo} das entidades de arquitetura que ficam sem projeto de propósito (cerebro.json, chave `sem_projeto`; padrão fnmatch)."""
    if regras is None:
        from hub.grafo_coleta import _cerebro
        regras = _cerebro().get('sem_projeto', [])
    out = {}
    for r in regras:
        for eid, n in ix.ent.items():
            if n['tipo'] in ARQUITETURA - {'vps'} and any(fnmatchcase(eid, p) for p in r.get('id', [])):
                out.setdefault(eid, r.get('motivo', ''))
    return out


def auditar(ix, hoje=None, regras=None):
    hoje = hoje or date.today()
    achados = []
    def add(tipo, ids, acao):
        achados.append({'tipo': tipo, 'nos': sorted(ids), 'acao': acao})
    if ix is None:
        return {'total': 0, 'baixo_grau': 0, 'percentual': 0, 'achados': [], 'limitacoes': ['Grafo indisponível.']}
    donos = projetos(ix)
    isentos = {eid: m for eid, m in excecoes(ix, regras).items() if eid not in donos}
    baixo = 0
    memorias = defaultdict(list)
    segredos = defaultdict(list)
    for eid, n in ix.ent.items():
        rs = ix.sai.get(eid, []) + ix.entra.get(eid, [])
        viz = {x for rel, x in rs if x != eid and rel != 'pertence_inferido'}
        tipos = {ix.ent[x]['tipo'] for x in viz}
        if len(viz) <= 1:
            baixo += 1; add('poucas_relacoes', [eid], 'Confirmar dono e entidades citadas; ligar apenas com evidência.')
        t = n['tipo']
        if eid in isentos:
            continue
        if t in ARQUITETURA - {'vps'} and eid not in donos:
            add('arquitetura_sem_projeto', [eid], 'Identificar projeto pelo caminho/comando; infraestrutura compartilhada pode permanecer sem projeto e oculta no mapa.')
        for origem, alvo in [('app', 'repo'), ('repo', 'app'), ('dominio', 'app')]:
            if t == origem and alvo not in tipos:
                add(f'{origem}_sem_{alvo}', [eid], f'Conferir inventário e registrar relação com {alvo}; pode ser exceção legítima.')
        if t == 'cron' and not tipos.intersection({'projeto', 'pessoa', 'repo', 'app'}):
            add('cron_sem_dono', [eid], 'Identificar responsável e registrar dono.')
        if t == 'memoria':
            copias = n.get('extra', {}).get('copias', [])
            if len(copias) > 1:
                distintos = len({x['hash'] for x in copias})
                add('memoria_copias_divergentes' if distintos > 1 else 'memoria_copias_iguais', [eid],
                    f'{len(copias)} cópias em contas/orch; comparar fontes e revisar possíveis contradições.' if distintos > 1 else f'{len(copias)} cópias idênticas; manter fonte canônica.')
            texto = re.sub(r'\s+', ' ', G.sem_acento(n.get('desc', ''))).strip()
            if texto:
                memorias[texto].append(eid)
        if t == 'segredo':
            h = n.get('extra', {}).get('valor_hash')
            if h and re.fullmatch(r'[a-fA-F0-9]{64}', str(h)):
                escopo = donos.get(eid)
                lote = n.get('extra', {}).get('lote_hmac')
                if escopo and lote:
                    segredos[(n['nome'], escopo, lote)].append((eid, h))
        if t == 'issue':
            extra = n.get('extra', {})
            criada = n.get('data', '')[:10]
            try:
                velha = (hoje - date.fromisoformat(criada)).days > 30
            except ValueError:
                velha = False
            if velha and extra.get('estado_tipo') in {'backlog', 'unstarted', 'started'} and 'pr' not in tipos:
                add('issue_antiga_sem_pr', [eid], 'Reavaliar prioridade e vincular PR existente ou encerrar com justificativa.')
    for ids in memorias.values():
        if len(ids) > 1:
            add('memoria_duplicada', ids, 'Revisar cópias entre contas e manter fonte canônica; arquivar só após revisão.')
    for itens in segredos.values():
        if len({h for _, h in itens}) > 1:
            add('segredo_divergente', [eid for eid, _ in itens], 'Responsável deve conferir escopo e rotação; valores nunca são exibidos.')
    total = len(ix.ent)
    arquitetura = [i for i, n in ix.ent.items() if n['tipo'] in ARQUITETURA - {'vps'}]
    sem_projeto = sum(i not in donos and i not in isentos for i in arquitetura)
    segs = [n for n in ix.ent.values() if n['tipo'] == 'segredo']
    issues = [n for n in ix.ent.values() if n['tipo'] == 'issue']
    return {'total': total, 'baixo_grau': baixo, 'percentual': round(100 * baixo / total, 2) if total else 0,
            'arquitetura': {'total': len(arquitetura), 'sem_projeto': sem_projeto, 'excecoes': len(isentos), 'percentual_sem_projeto': round(100 * sem_projeto / len(arquitetura), 2) if arquitetura else 0},
            'cobertura': {'segredos_com_hash': sum(bool(n.get('extra', {}).get('valor_hash')) for n in segs), 'segredos': len(segs), 'issues_com_estado': sum(bool(n.get('extra', {}).get('estado_tipo')) for n in issues), 'issues': len(issues)},
            'achados': achados, 'limitacoes': ['Contradições semânticas exigem revisão humana.', 'Segredos: HMAC efêmero por coleta root e projeto identificado; arquivos locais, remotos, multilinha e interpolação podem ficar sem cobertura.', 'Issues: somente estado estruturado conhecido.']}
