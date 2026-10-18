import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hub import agenda, cerebro, config, conta, debugger, incidentes

NOW = dt.datetime(2026, 10, 15, 20, tzinfo=dt.timezone.utc)
OLD = conta.iso(NOW - dt.timedelta(minutes=65))
REPO = config.repo('test')


class DebuggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.notas = self.base / 'notas-tecnicas.jsonl'
        env = patch.dict('os.environ', {'NOTAS_TECNICAS_ARQ': str(self.notas)})
        env.start()
        self.addCleanup(env.stop)

    def item(self, **kw):
        return dict(repo=REPO, alvo='pr:1', erro='teste falhou', desde=OLD, tipo='ci', pronto=True, **kw)

    def vigiar(self, itens, minuto=0):
        with patch.object(agenda, 'rodando_todas', return_value={}):
            debugger.vigiar(self.base, NOW + dt.timedelta(minutes=minuto), lambda **_: itens)

    def gh(self, commit=OLD, estados=None):
        pr = {'number': 1, 'head': {'sha': 'abc', 'ref': 'codex/krn-897'}, 'body': 'Fixes KRN-897', 'html_url': 'https://github.com/org/test/pull/1'}
        status = estados or [{'state': 'failure', 'context': 'local-ci', 'description': 'falhou em npm test: a.py (2s)', 'created_at': OLD}]
        def get(url):
            if url == 'repos/' + REPO:
                return {'default_branch': 'develop'}
            if '/pulls?' in url:
                return [pr]
            if '/statuses?' in url:
                return status
            return {'sha': 'abc', 'commit': {'committer': {'date': commit}}}
        return get

    def test_pr_parado_main_e_commit_recente(self):
        itens = debugger.coletar_repo(REPO, self.gh(), NOW, self.base)
        self.assertEqual([i['pronto'] for i in itens], [True, True])
        self.assertEqual(itens[0]['branch'], 'develop')
        self.assertEqual(itens[1]['issue'], 'KRN-897')
        itens = debugger.coletar_repo(REPO, self.gh(conta.iso(NOW)), NOW, self.base)
        self.assertFalse(itens[1]['pronto'])
        self.assertTrue(itens[0]['pronto'])

    def test_duas_rodadas_mesmo_erro_disparam_antes_de_30_min(self):
        s = {'state': 'failure', 'context': 'local-ci', 'description': 'a.py (2s)', 'created_at': conta.iso(NOW)}
        itens = debugger.coletar_repo(REPO, self.gh(conta.iso(NOW), [s, {**s, 'description': 'a.py (3s)'}]), NOW, self.base)
        self.assertTrue(itens[1]['pronto'])

    def test_alerta_velho_e_incidente(self):
        p = self.base / 'alertas/fsite05/a.json'
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'desde': OLD, 'estado': 'falha', 'mensagem': 'CRM caiu'}))
        itens = debugger.coletar(self.base, self.gh(), NOW, repos=['test'])
        alertas = [i for i in itens if i['tipo'] == 'alerta']
        self.assertEqual(len(alertas), 1)
        self.vigiar(alertas)
        self.assertEqual(len(agenda.fila(self.base)), 1)
        p.write_text(json.dumps({'desde': OLD, 'estado': 'falha', 'tratado': {'resolvido': True}}))
        self.assertFalse(any(i['tipo'] == 'alerta' for i in debugger.coletar(self.base, self.gh(), NOW, repos=['test'])))

    def test_falha_do_bot_vira_item_e_nota_nao(self):
        ha = lambda m: conta.iso(NOW - dt.timedelta(minutes=m))
        self.notas.write_text(''.join(json.dumps(r) + '\n' for r in [
            {'em': ha(60), 'tipo': 'falha', 'onde': 'Secretaria - Cliente A', 'texto': 'deploy não subiu'},
            {'em': ha(50), 'tipo': 'nota', 'onde': 'Secretaria - Cliente A', 'texto': 'PR mergeado'},
            {'em': ha(60 * 30), 'tipo': 'falha', 'onde': 'velha', 'texto': 'de ontem'}]))
        itens = [i for i in debugger.coletar(self.base, self.gh(), NOW, repos=[]) if i['alvo'].startswith('bot-falha:')]
        self.assertEqual(len(itens), 1)
        self.assertEqual((itens[0]['tipo'], itens[0]['repo']), ('alerta', config.repo('bot_whatsapp')))
        self.assertIn('deploy não subiu', itens[0]['erro'])
        self.vigiar(itens)
        self.assertEqual(len(agenda.fila(self.base)), 1)

    def test_teto_tres_uma_tarefa_por_repo_e_diario(self):
        itens = [self.item(), {**self.item(), 'alvo': 'pr:2'}]
        self.vigiar(itens)
        self.vigiar(itens, 5)
        self.assertEqual(len(agenda.fila(self.base)), 1)
        self.assertEqual(sum(i['tentativas'] for i in debugger.ler(self.base)['itens'].values()), 1)
        for minuto in (11, 22, 33, 44):
            agenda.tirar(agenda.fila(self.base)[0]['nome'], self.base)
            self.vigiar([itens[0]], minuto)
            if minuto >= 33:
                break
        i = debugger.ler(self.base)['itens'][debugger.chave(itens[0])]
        self.assertEqual(i['tentativas'], 3)
        self.assertIn('escalado_em', i)
        diario = (self.base / 'consertos.jsonl').read_text()
        self.assertEqual(diario.count('precisa_fulano'), 1)

    def test_no_medidas_e_contadores(self):
        self.vigiar([self.item()])
        n = next(n for n in cerebro.nos(self.base, NOW) if n['id'] == 'debugger')
        self.assertTrue(n['alerta'])
        self.assertEqual(n['medidas']['ci_aberto'], 1)
        self.assertIn(('ci', 'debugger'), cerebro.ARESTAS)
        self.vigiar([], 5)
        m = debugger.painel(self.base, NOW + dt.timedelta(minutes=5))
        self.assertEqual(m['ci_aberto'], 0)
        self.assertEqual(m['ci_resolvido_24h'], 1)
        self.assertEqual(m['ci_media_min'], 70)

    def test_fila_persiste_executor_e_prompt(self):
        self.vigiar([self.item()])
        t = next(iter(agenda.da_fila(self.base).values()))
        self.assertEqual(agenda.faixa_de(t), 'codex')
        self.assertIn('tentativa 1/3', t.prompt)
        self.assertIn('MESMA branch', t.prompt)

    def test_agendar_nao_tira_o_debugger_da_fila(self):
        self.vigiar([self.item()])
        agenda.agendar({}, conta.agora(), self.base)  # 16/10: saía como "tarefa não existe mais" a cada minuto
        self.assertTrue(any(i.get("debugger") for i in agenda.fila(self.base)))

    def test_fonte_fora_nao_resolve_vermelho(self):
        self.vigiar([self.item()])
        def fora(_):
            raise OSError('rede')
        foto = debugger.coletar(self.base, fora, NOW, repos=['test'])
        self.assertEqual(len(foto), 1)
        self.vigiar(foto, 5)
        i = debugger.ler(self.base)['itens'][debugger.chave(self.item())]
        self.assertNotIn('resolvido_em', i)
        self.assertEqual(debugger.painel(self.base, NOW)['ci_aberto'], 1)

    def test_fallback_so_sem_login_e_repo_ocupado(self):
        self.vigiar([self.item()])
        with patch.object(agenda, 'rodando_todas', return_value={}):
            self.assertIsNone(agenda.proximo({}, NOW, False, self.base, so_codex=False, codex_login=True, so_api=False))
            t = agenda.proximo({}, NOW, False, self.base, so_codex=False, codex_login=False, so_api=False)
            self.assertEqual(agenda.faixa_de(t), 'construtor')
            self.assertIsNotNone(agenda.proximo({}, NOW, False, self.base, so_codex=True, so_api=False))
            self.assertIsNone(agenda.proximo({}, NOW, False, self.base, ocupados=[REPO], so_codex=True, so_api=False))

    def test_recibo_tratado_fecha_sem_editar_fonte(self):
        i = {**self.item(), 'tipo': 'alerta'}
        self.vigiar([i])
        debugger.tratar(debugger.chave(i), 'corrigi e conferi', self.base, NOW)
        self.vigiar([i], 5)
        self.assertFalse(debugger.painel(self.base, NOW)['abertos'])
        self.assertEqual(debugger.painel(self.base, NOW)['alerta_media_min'], 65)

    def test_mensagem_muda_sem_zerar_tentativas(self):
        for n in range(4):
            if agenda.fila(self.base):
                agenda.tirar(agenda.fila(self.base)[0]['nome'], self.base)
            self.vigiar([{**self.item(), 'erro': f'teste {n} falhou'}], n * 11)
        i = debugger.ler(self.base)['itens'][debugger.chave(self.item())]
        self.assertEqual(i['tentativas'], 3)
        self.assertIn('escalado_em', i)
        self.assertFalse(agenda.fila(self.base))

    def test_pr_fechado_vermelho_nao_e_conserto(self):
        self.vigiar([self.item()])
        original = self.gh()
        def gh(url):
            if '/pulls?' in url:
                return []
            if '/pulls/1' in url:
                return {'head': {'sha': 'abc'}, 'state': 'closed'}
            if '/statuses?' in url:
                return [{'context': 'local-ci', 'state': 'failure', 'created_at': OLD}]
            return original(url)
        foto = debugger.coletar(self.base, gh, NOW, repos=['test'])
        self.vigiar(foto, 5)
        i = debugger.ler(self.base)['itens'][debugger.chave(self.item())]
        self.assertIn('encerrado_em', i)
        self.assertNotIn('resolvido_em', i)
        self.assertEqual(debugger.painel(self.base, NOW)['ci_resolvido_24h'], 0)
        self.assertNotIn('consertado', (self.base / 'consertos.jsonl').read_text())

    def test_json_invalido_nao_derruba_fontes_saudaveis(self):
        for nome, conteudo in [('alertas/fsite05/quebrado.json', '{'),
                                ('alertas/fsite05/incompleto.json', '{}'),
                                ('vigia-clientej/config.json', '{"desde": "invalido"}'),
                                ('claude/main-vermelho/repo.json', '[]'),
                                ('alertas/estado.json', '{"itens": {"x": {}}}')]:
            p = self.base / nome
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(conteudo)
        foto = debugger.coletar(self.base, self.gh(), NOW, repos=['test'])
        self.assertEqual(len(foto), 2)
        self.vigiar(foto)
        self.assertEqual(len(agenda.fila(self.base)), 1)

    def test_fonte_alerta_corrompida_conserva_aberto(self):
        p = self.base / 'alertas/fsite05/a.json'
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'desde': OLD, 'estado': 'falha', 'mensagem': 'CRM caiu'}))
        foto = debugger.coletar(self.base, self.gh(), NOW, repos=[])
        self.vigiar(foto)
        p.write_text('{')
        foto = debugger.coletar(self.base, self.gh(), NOW, repos=[])
        self.vigiar(foto, 5)
        self.assertEqual(len(debugger.painel(self.base, NOW)['abertos']), 1)


HUB = 'example-org/repo-privado'


def gh_prs(**por_numero):
    """GitHub falso: pulls/<n> → o dict dado; lista de abertos em `abertos`."""
    abertos = por_numero.pop('abertos', [])
    def get(url):
        if '/pulls?' in url:
            return abertos
        n = int(url.rsplit('/', 1)[1])
        return por_numero[f'p{n}']
    return get


class PrAbertoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def alerta(self):
        i = dict(repo=HUB, alvo='INC-62', erro='parou sem recibo', desde=OLD, tipo='alerta', pronto=True, tentativas=0)
        with patch.object(agenda, 'rodando_todas', return_value={}):
            debugger.vigiar(self.base, NOW, lambda **_: [i])
        return debugger.chave(i)

    def test_tratar_com_pr_aberto_recusa(self):
        k = self.alerta()
        gh = gh_prs(p414={'state': 'open', 'merged': False})
        for nota in ('conserto no PR #414', f'conserto em https://github.com/{HUB}/pull/414', 'PR 414 pronto'):
            with self.assertRaises(ValueError):
                debugger.tratar(k, nota, self.base, NOW, gh)
        self.assertNotIn('tratado', debugger.ler(self.base)['itens'][k])

    def test_tratar_pr_mergeado_exige_atualizar_hub_rc0(self):
        k = self.alerta()
        gh = gh_prs(p414={'state': 'closed', 'merged': True})
        with self.assertRaises(ValueError):
            debugger.tratar(k, 'PR #414 mergeado', self.base, NOW, gh)
        debugger.tratar(k, 'PR #414 mergeado; atualizar-hub rc=0', self.base, NOW, gh)
        self.assertIn('tratado', debugger.ler(self.base)['itens'][k])

    def test_tratar_pr_fechado_sem_merge_recusa(self):
        k = self.alerta()
        gh = gh_prs(p414={'state': 'closed', 'merged': False})
        with self.assertRaises(ValueError):
            debugger.tratar(k, 'conserto no PR #414; atualizar-hub rc=0', self.base, NOW, gh)

    def test_tratar_sem_mudanca_de_codigo_e_sem_pr(self):
        k = self.alerta()
        debugger.tratar(k, 'sem mudança de código: era cache; issue #5', self.base, NOW, gh_prs())
        self.assertIn('tratado', debugger.ler(self.base)['itens'][k])

    def test_numero_que_e_issue_nao_barra(self):
        import subprocess
        def gh(url):
            raise subprocess.CalledProcessError(1, 'gh', stderr='gh: Not Found (HTTP 404)')
        k = self.alerta()
        debugger.tratar(k, 'conferi a issue #5 e o log', self.base, NOW, gh)

    def test_incidente_feito_com_pr_aberto_recusa(self):
        inc = incidentes.registrar('parou', 'pronto_sem_recibo', 'hub/recibo.py', base_dados=self.base, agora=NOW)
        gh = gh_prs(p414={'state': 'open', 'merged': False})
        with self.assertRaises(ValueError):
            incidentes.marcar(inc['id'], 'feito', 'CI/merge/deploy pendentes: PR #414; tests/test_recibo.py', self.base, NOW, gh)
        self.assertEqual(incidentes.listar(base_dados=self.base)[0]['estado'], 'pendente')


class UmPrPorClasseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def coletar(self, abertos=()):
        gh = gh_prs(abertos=list(abertos))
        with patch('hub.sites.ativos', return_value=[]):
            return [i for i in debugger.coletar(self.base, gh, NOW, repos=[]) if i['tipo'] == 'alerta']

    def test_segundo_incidente_da_classe_usa_a_mesma_branch(self):
        a = incidentes.registrar('parou 1', 'pronto_sem_recibo', 'hub/recibo.py', base_dados=self.base, agora=NOW)
        b = incidentes.registrar('parou 2', 'pronto_sem_recibo', 'hub/linear.py', base_dados=self.base, agora=NOW)
        c = incidentes.registrar('outro', 'tarefa_parada', 'hub/parada.py', base_dados=self.base, agora=NOW)
        itens = {i['alvo']: i for i in self.coletar()}
        self.assertEqual(itens[a['id']]['branch_conserto'], itens[b['id']]['branch_conserto'])
        self.assertNotEqual(itens[a['id']]['branch_conserto'], itens[c['id']]['branch_conserto'])
        self.assertIn(itens[a['id']]['branch_conserto'], debugger.tarefa({**itens[b['id']], 'tentativas': 1}).prompt)

    def test_pr_aberto_da_classe_recebe_o_conserto_e_segura_novo_disparo(self):
        incidentes.registrar('parou 1', 'pronto_sem_recibo', 'hub/recibo.py', base_dados=self.base, agora=NOW - dt.timedelta(minutes=65))
        pr = {'number': 414, 'title': 'fix: INC-62: pronto_sem_recibo', 'body': '', 'html_url': f'https://github.com/{HUB}/pull/414',
              'head': {'ref': 'codex/inc-62-recibo'}}
        i, = self.coletar([pr])
        self.assertEqual((i['pr_aberto'], i['branch_conserto']), (pr['html_url'], 'codex/inc-62-recibo'))
        self.assertIn('Não abra outro PR', debugger.tarefa({**i, 'tentativas': 1}).prompt)
        with patch.object(agenda, 'rodando_todas', return_value={}):
            debugger.vigiar(self.base, NOW, lambda **_: [i])  # 1ª tentativa: soma no PR
            self.assertEqual(len(agenda.fila(self.base)), 1)
            agenda.tirar(agenda.fila(self.base)[0]['nome'], self.base)
            debugger.vigiar(self.base, NOW + dt.timedelta(minutes=11), lambda **_: [i])  # PR segue aberto: o hub segue o PR
        self.assertFalse(agenda.fila(self.base))
        self.assertEqual(debugger.ler(self.base)['itens'][debugger.chave(i)]['tentativas'], 1)


SHA = '392907284aeecef990cf13655d6441a2a2f719a8'
LIMPA = {'body': f'**Revisão do Codex** (commit `{SHA[:8]}`)\n\nSem achados sérios.', 'created_at': '2026-10-12T12:30:00Z'}


class MergeDoDebuggerTest(unittest.TestCase):
    def setUp(self):
        from hub import automerge
        self.automerge = automerge
        self.tmp = tempfile.TemporaryDirectory()
        self.diff = "+ok\n"
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.logs, self.merges, self.pedidos = [], [], []
        self.ci, self.comentarios = 'success', [LIMPA]
        self.pr = {'number': 414, 'draft': False, 'title': 'fix: INC-62: pronto_sem_recibo', 'body': 'INC-88',
                   'head': {'ref': 'codex/debugger-pronto-sem-recibo', 'sha': SHA}}

    def gh(self, url):
        if '/pulls?state=open' in url:
            return [self.pr]
        if url.endswith('/check-runs'):
            return {'check_runs': []}
        if url.endswith(f'/commits/{SHA}/status'):
            return {'statuses': [{'context': 'local-ci', 'state': self.ci}]}
        if '/pulls?state=closed' in url:
            return [{'number': 400, 'head': {'sha': 'a' * 40}, 'merged_at': '2026-10-11T10:00:00Z'}]
        if url.endswith('/commits/' + 'a' * 40 + '/status'):
            return {'statuses': [{'context': 'local-ci', 'state': 'success'}]}
        if '/issues/414/comments' in url:
            return self.comentarios
        raise AssertionError(url)

    def rodar(self):
        def rodar_gh(args):
            self.merges.append(args)
            return 0, ''
        def pedir(acao, **kw):
            self.pedidos.append(acao)
            return 'v-1'
        return self.automerge.vigiar_debugger(self.gh, self.base, self.logs.append, rodar_gh, pedir, lambda r, n: self.diff,
                                              repos=['repo-privado'])

    def test_pr_verde_do_debugger_e_mergeado_pelo_hub(self):
        self.assertEqual(self.rodar(), [(HUB, 414)])
        self.assertIn('--match-head-commit', self.merges[0])
        self.assertEqual(self.pedidos, ['atualizar-hub'])
        self.assertEqual(self.rodar(), [])  # mesma ponta: não repete
        self.assertEqual(len(self.merges), 1)

    def test_prompt_vigiado_sem_evals_verde_nao_vai(self):
        """o PR do Debugger que mexe em prompt vigiado também espera o status `evals` da ponta."""
        if not self.automerge.vigia:
            self.skipTest("portão de evals é do privado: fora do snapshot público não existe")
        self.diff = "--- a/hub/linear.py\n+++ b/hub/linear.py\n@@ -1 +1 @@\n-a\n+b\n"
        self.assertEqual(self.rodar(), [])
        self.assertFalse(self.merges)
        self.assertTrue(any('evals' in l for l in self.logs))

    def test_vermelho_ou_revisao_com_achado_nao_vai(self):
        self.ci = 'failure'
        self.assertEqual(self.rodar(), [])
        self.ci, self.comentarios = 'success', [{**LIMPA, 'body': LIMPA['body'] + '\n[P1] bug'}]
        self.assertEqual(self.rodar(), [])
        self.assertFalse(self.merges)

    def test_pr_que_nao_e_do_debugger_nao_vai(self):
        self.pr = {**self.pr, 'title': 'feat: outra coisa', 'head': {'ref': 'krn-9', 'sha': SHA}}
        self.assertEqual(self.rodar(), [])

    def test_incidentes_so_ficam_feitos_com_atualizar_hub_rc0(self):
        for n in (62, 88):
            incidentes.registrar(f'f{n}', 'pronto_sem_recibo', 'hub/recibo.py', base_dados=self.base, agora=NOW)
        self.rodar()
        def sem(pid):
            return None
        self.automerge.fechar_incidentes(self.base, self.logs.append, sem, NOW)
        self.assertEqual({i['estado'] for i in incidentes.listar(base_dados=self.base)}, {'pendente'})
        self.automerge.fechar_incidentes(self.base, self.logs.append, lambda pid: {'codigo': 1}, NOW)
        self.assertEqual({i['estado'] for i in incidentes.listar(base_dados=self.base)}, {'pendente'})

    def test_atualizar_hub_rc0_sem_teste_no_diff_fica_pendente(self):
        # incidente só fecha com o teste de regressão citado; o PR sem teste deixa pendente
        self.diff = '+++ b/hub/recibo.py\n+ok\n'
        self.pr = {**self.pr, 'title': 'fix: INC-1: pronto_sem_recibo', 'body': ''}
        incidentes.registrar('f1', 'pronto_sem_recibo', 'hub/recibo.py', base_dados=self.base, agora=NOW)
        self.rodar()
        with patch('hub.esperas.gh_api', return_value={'state': 'closed', 'merged': True}):
            self.automerge.fechar_incidentes(self.base, self.logs.append, lambda pid: {'codigo': 0}, NOW)
        self.assertEqual({i['estado'] for i in incidentes.listar(base_dados=self.base)}, {'pendente'})
        self.assertTrue(any('teste de regressão' in l for l in self.logs))

    def test_atualizar_hub_rc0_marca_feito(self):
        self.diff = '+++ b/hub/recibo.py\n+ok\n+++ b/tests/test_recibo.py\n+ok\n'
        self.pr = {**self.pr, 'title': 'fix: INC-1, INC-2: pronto_sem_recibo', 'body': ''}
        for n in (1, 2):
            incidentes.registrar(f'f{n}', 'pronto_sem_recibo', 'hub/recibo.py', base_dados=self.base, agora=NOW)
        self.rodar()
        with patch('hub.esperas.gh_api', return_value={'state': 'closed', 'merged': True}):
            self.automerge.fechar_incidentes(self.base, self.logs.append, lambda pid: {'codigo': 0}, NOW)
        self.assertEqual({i['estado'] for i in incidentes.listar(base_dados=self.base)}, {'feito'})

    def test_pedido_que_falha_apos_o_merge_e_refeito_no_proximo_tick(self):
        incidentes.registrar('f62', 'pronto_sem_recibo', 'hub/recibo.py', base_dados=self.base, agora=NOW)
        def pedir_falha(acao, **kw):
            raise RuntimeError('fila indisponível')
        def rodar_gh(args):
            return 0, ''
        self.automerge.vigiar_debugger(self.gh, self.base, self.logs.append, rodar_gh, pedir_falha, lambda r, n: '+ok\n',
                                       repos=['repo-privado'])
        pendentes = self.automerge._estado(self.base)['_pos_merge']
        self.assertEqual(len(pendentes), 1)
        self.assertTrue(pendentes[0]['pede'])
        refeitos = []
        def pedir(acao, **kw):
            refeitos.append(acao)
            return 'v-2'
        self.automerge.fechar_incidentes(self.base, self.logs.append, lambda pid: None, NOW, pedir=pedir)
        self.assertEqual(refeitos, ['atualizar-hub'])
        self.assertEqual(self.automerge._estado(self.base)['_pos_merge'][0]['pedido'], 'v-2')
        self.assertEqual({i['estado'] for i in incidentes.listar(base_dados=self.base)}, {'pendente'})
