"""Fila do revisor Codex e recibos de merges confirmados."""
import datetime as dt
import json
from pathlib import Path
from hub import data, linear


def em_revisao(consulta=None):
    consulta = consulta or linear.gql
    itens, cursor = [], None
    while True:
        r = consulta('''query($c: String) { issues(first: 100, after: $c,
          filter: {state: {name: {eq: "In Review"}}}) {
          nodes { id identifier title createdAt description labels { nodes { name } }
            attachments { nodes { url } } }
          pageInfo { hasNextPage endCursor } } }''', c=cursor)['issues']
        itens.extend(r['nodes'])
        if not r['pageInfo']['hasNextPage']:
            return sorted(itens, key=lambda i: (i['createdAt'], i['identifier']))
        cursor = r['pageInfo']['endCursor']


def arquivo():
    return data() / 'claude' / 'codex-merges.jsonl'


def merges_hoje(agora=None, caminho=None):
    hoje = (agora or dt.datetime.now(dt.timezone.utc)).date().isoformat()
    try:
        linhas = Path(caminho or arquivo()).read_text().splitlines()
    except FileNotFoundError:
        return 0
    prs = set()
    for linha in linhas:
        try:
            r = json.loads(linha)
            if r.get('em', '').startswith(hoje):
                prs.add(r['pr'])
        except (ValueError, KeyError):
            continue
    return len(prs)


def main():
    itens = em_revisao()
    print(json.dumps({'in_review': len(itens), 'candidatas': [i for i in itens if
          'merge' in {l['name'] for l in i['labels']['nodes']}]}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
