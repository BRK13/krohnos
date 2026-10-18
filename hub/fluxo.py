"""rascunho visual; nenhuma escrita na configuração da operação."""
import json
from hub import RAIZ, config, conta, roteamento


def importar():
    """Projeção explícita dos campos públicos; nunca serializa hub.json inteiro."""
    cfg = config.ler_hub_json()
    modelos = config.definido('agentes.modelos') or {}
    nos = [{'id': 'fila', 'nome': 'Fila · Linear', 'papel': 'origem', 'modelo': '', 'x': 40, 'y': 225}]
    arestas = []
    for i, n in enumerate(conta.contas_config()):
        ident = 'c' + n
        nos.append({'id': ident, 'nome': 'Conta ' + n, 'papel': conta.faixa(n) or 'sem faixa',
                    'modelo': modelos.get(n) or roteamento.config().get('padrao', 'sonnet'), 'x': 320, 'y': 35 + i * 140})
        arestas.extend([['fila', ident], [ident, 'revisao']])
    nos.extend([{'id': ident, 'nome': nome, 'papel': papel, 'modelo': modelo, 'x': x, 'y': y}
                for ident, nome, papel, modelo, x, y in [
                    ('revisao', 'Revisão Codex', 'revisor', (cfg.get('codex') or {}).get('modelo') or 'codex', 610, 225),
                    ('ci', 'CI', 'etapa', '', 870, 225), ('deploy', 'Deploy', 'etapa', '', 1130, 225)]])
    arestas.extend([['revisao', 'ci'], ['ci', 'deploy']])
    return {'nos': nos, 'arestas': arestas, 'roteamento': roteamento.tabela(),
            'nota': 'Contas, faixas e modelos importados. Etapas representam o caminho do Cérebro; não são ainda uma configuração executável.'}


def tela():
    dados = json.dumps(importar(), ensure_ascii=False).replace('<', '\\u003c')
    return (RAIZ / 'hub' / 'fluxo.html').read_text().replace('/*DADOS*/null', dados)
