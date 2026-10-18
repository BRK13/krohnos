import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from hub import agenda, revisor_merge, tarefas

# a tarefa real mora no repo privado; a cópia em fixtures vale para o contrato (só Codex, sem API)
FIXTURE = tarefas.carregar(pasta=Path(__file__).resolve().parent / 'fixtures' / 'tarefas', extra=False)


class RevisorMergeTest(unittest.TestCase):
    def test_paginacao_e_ordem(self):
        def consulta(q, c=None):
            return {'issues': {'nodes': [{'identifier': 'B' if c is None else 'A',
                'createdAt': '2026-10-15' if c is None else '2026-10-14'}],
                'pageInfo': {'hasNextPage': c is None, 'endCursor': 'pagina2'}}}
        self.assertEqual([i['identifier'] for i in revisor_merge.em_revisao(consulta)], ['A', 'B'])

    def test_contagem_deduplica_e_ignora_outro_dia(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'merges.jsonl'
            p.write_text('\n'.join(json.dumps(r) for r in [
                {'em': '2026-10-15T12:00:00Z', 'pr': 'a'},
                {'em': '2026-10-15T13:00:00Z', 'pr': 'a'},
                {'em': '2026-10-14T13:00:00Z', 'pr': 'b'}]) + '\nquebrado')
            self.assertEqual(revisor_merge.merges_hoje(dt.datetime(2026, 10, 15), p), 1)

    def test_tarefa_somente_codex(self):
        t = FIXTURE['codex-revisor-merge']
        self.assertEqual(t.conta, 'codex')
        self.assertEqual(agenda.faixa_de(t), 'codex')
        self.assertFalse(agenda.quer_api(t))

    def test_sem_login_nao_cai_no_claude_nem_api(self):
        from unittest import mock
        t = FIXTURE['codex-revisor-merge']
        with tempfile.TemporaryDirectory() as d:
            agenda.enfileirar(t, 'teste', d)
            with mock.patch.object(agenda, 'minha_faixa', return_value=None):
                self.assertIsNone(agenda.proximo({t.nome: t}, agenda.conta.agora(), False,
                                                d, so_codex=False, so_api=False, codex_login=False))
                self.assertIsNone(agenda.proximo({t.nome: t}, agenda.conta.agora(), False,
                                                d, so_codex=False, so_api=True, codex_login=False))
