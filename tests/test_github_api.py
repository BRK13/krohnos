import json
import subprocess
import tempfile
import unittest
from unittest.mock import Mock
from hub import github_api as g, incidentes


_ENV = None


def setUpModule():
    """Sem `gh auth token` real e sem memo de credencial vazando entre testes."""
    global _ENV
    from unittest import mock
    from hub import github_api
    _ENV = mock.patch.dict("os.environ", {"GH_TOKEN": "token-de-teste"})
    _ENV.start()
    github_api._AUTH.clear()


def tearDownModule():
    from hub import github_api
    _ENV.stop()
    github_api._AUTH.clear()


def _rl(restante, limite=5000, reset=9000):
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(
        {'resources': {'core': {'limit': limite, 'remaining': restante, 'reset': reset}}}))


class AmostradorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_banco_travado_tenta_de_novo(self):
        import sqlite3
        from unittest import mock
        chamadas = []

        def fn():
            chamadas.append(1)
            if len(chamadas) < 3:
                raise sqlite3.OperationalError('database is locked')
            return 'ok'
        with mock.patch('hub.github_api.time.sleep') as dorme:
            self.assertEqual(g._com_tentativas(fn), 'ok')
        self.assertEqual(dorme.call_count, 2)
        with mock.patch('hub.github_api.time.sleep'), self.assertRaises(sqlite3.OperationalError):
            g._com_tentativas(Mock(side_effect=sqlite3.OperationalError('database is locked')))
        with mock.patch('hub.github_api.time.sleep') as dorme, self.assertRaises(sqlite3.OperationalError):
            g._com_tentativas(Mock(side_effect=sqlite3.OperationalError('no such table')))
        dorme.assert_not_called()

    def test_amostra_app_e_gh_sem_gravar_token(self):
        from unittest import mock
        run = Mock(side_effect=[_rl(4000), _rl(300)])
        with mock.patch('hub.github_app.token', return_value='token-da-app'):
            feitas = g.amostrar_cota(self.tmp.name, run, agora=1000)
        self.assertEqual([f[0] for f in feitas], ['App', 'gh pessoal'])
        self.assertEqual(run.call_args_list[0].args[0], ['gh', 'api', 'rate_limit'])
        self.assertEqual(run.call_args_list[0].kwargs['env']['GH_TOKEN'], 'token-da-app')
        con = g._banco(self.tmp.name)
        hist = con.execute('SELECT hora, restante, limite FROM cota_hist ORDER BY restante').fetchall()
        con.close()
        self.assertEqual(hist, [(1000, 300, 5000), (1000, 4000, 5000)])
        self.assertEqual(sorted(c[1] for c in g.cotas(self.tmp.name, agora=1000)), ['App', 'gh pessoal'])
        self.assertNotIn('token-', repr(g.cotas(self.tmp.name, agora=1000)) + repr(hist))
        inc = incidentes.listar(base_dados=self.tmp.name)
        self.assertEqual(len(inc), 1)  # só a credencial com 300 restantes
        g.amostrar_cota(self.tmp.name, Mock(side_effect=[_rl(4000), _rl(290)]), agora=1300)
        self.assertEqual(len(incidentes.listar(base_dados=self.tmp.name)), 1)  # mesma chave (reset igual)

    def test_retencao_8_dias_e_falha_ignorada(self):
        from unittest import mock
        with mock.patch('hub.github_app.token', return_value=None):
            g.amostrar_cota(self.tmp.name, Mock(return_value=_rl(4000)), agora=1000)
            run = Mock(return_value=subprocess.CompletedProcess([], 1, stdout='erro'))
            self.assertEqual(g.amostrar_cota(self.tmp.name, run, agora=1000 + 9*86400), [])
        con = g._banco(self.tmp.name)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM cota_hist').fetchone()[0], 0)
        con.close()

    def test_poda_vencidas_e_listagem_fresca(self):
        from unittest import mock
        with mock.patch('hub.github_app.token', return_value=None):
            g.amostrar_cota(self.tmp.name, Mock(return_value=_rl(4000, reset=1500)), agora=1000)
            self.assertEqual(len(g.cotas(self.tmp.name, agora=1000 + 7200)), 1)
            self.assertEqual(g.cotas(self.tmp.name, agora=1000 + 7201), [])  # sem amostra fresca
            os_env = {'GH_TOKEN': 'outro-token'}
            with mock.patch.dict('os.environ', os_env):
                g.amostrar_cota(self.tmp.name, Mock(return_value=_rl(4000, reset=99999)), agora=1500 + 7201)
        con = g._banco(self.tmp.name)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM cota').fetchone()[0], 1)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM credencial').fetchone()[0], 1)
        con.close()

    def test_cli_sai_com_erro_sem_credencial_amostrada(self):
        from unittest import mock
        import sys
        with mock.patch('subprocess.run', side_effect=OSError), mock.patch.object(sys, 'argv', ['x', 'amostrar']):
            with self.assertRaises(SystemExit) as c:
                import runpy
                runpy.run_module('hub.github_api', run_name='__main__')
        self.assertNotEqual(c.exception.code, 0)

    def test_root_devolve_o_banco_ao_claude(self):
        from unittest import mock
        import types
        chown = mock.Mock()
        pw = types.SimpleNamespace(pw_uid=1001, pw_gid=1002)
        with mock.patch('os.geteuid', return_value=0), mock.patch('pwd.getpwnam', return_value=pw), \
                mock.patch('os.chown', chown):
            g._banco(self.tmp.name).close()
        nomes = {c.args[0] for c in chown.call_args_list if isinstance(c.args[0], str)}
        self.assertIn('consumo.sqlite3', nomes)
        self.assertEqual(len(chown.call_args_list) >= 2, True)  # a pasta (por fd) e o banco
        for c in chown.call_args_list:
            self.assertEqual(c.args[1:], (1001, 1002))
        self.assertTrue(all(c.kwargs.get('follow_symlinks', True) is False
                            for c in chown.call_args_list if isinstance(c.args[0], str)))
        with mock.patch('os.geteuid', return_value=0), mock.patch('pwd.getpwnam', side_effect=KeyError('claude')):
            g._banco(self.tmp.name).close()  # sem o usuário: segue

    def test_credenciais_incluem_token_do_hub_e_ambiente(self):
        from unittest import mock
        with mock.patch('hub.github_app.token', return_value=None), \
                mock.patch('hub.repos.token_github', return_value='token-do-hub'):
            rot = [(r, t) for r, t in g._credenciais(Mock())]
        self.assertEqual(rot, [('gh pessoal', 'token-de-teste'), ('gh hub', 'token-do-hub')])

    def test_cli_falha_repetida_sai_zero_no_mesmo_dia(self):
        from unittest import mock
        import runpy
        import sys
        codigos = []
        for _ in range(3):
            with mock.patch('subprocess.run', side_effect=OSError), mock.patch.object(sys, 'argv', ['x', 'amostrar']), \
                    mock.patch.dict('os.environ', {'HUB_DATA': self.tmp.name}), \
                    mock.patch('hub.github_app.token', return_value=None):
                try:
                    runpy.run_module('hub.github_api', run_name='__main__')
                    codigos.append(0)
                except SystemExit as e:
                    codigos.append(e.code)
        self.assertNotEqual(codigos[0], 0)
        self.assertEqual(codigos[1:], [0, 0])

    def test_banco_recusa_link_simbolico_plantado(self):
        import os
        alvo = os.path.join(self.tmp.name, 'privilegiado')
        pasta = g._banco_pasta(self.tmp.name)
        os.symlink(alvo, pasta / 'consumo.sqlite3')
        with self.assertRaises(OSError):
            g._banco(self.tmp.name)
        self.assertFalse(os.path.exists(alvo))
        g.registrar_http('x', 't', 200, self.tmp.name)  # melhor-esforço: não levanta

    def test_banco_sem_permissao_nao_derruba_get_nem_leituras(self):
        from unittest import mock
        run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=(
            'HTTP/2.0 200 OK\nX-RateLimit-Remaining: 900\nX-RateLimit-Reset: 3000\n\n{"ok": 1}')))
        with mock.patch('hub.github_api._banco', side_effect=PermissionError('root')):
            self.assertEqual(g.get('repos/o/r/pulls/1', base_dados=self.tmp.name, agora=1000, rodar=run, token='t'), {'ok': 1})
            g.registrar_http('x', 't', 200, self.tmp.name)
            self.assertEqual(g.resumo(self.tmp.name), [])
            self.assertEqual(g.cotas(self.tmp.name), [])

    def test_pivo_ordena_por_total(self):
        linhas = [('a', 'http', 200, 2), ('a', 'http', 304, 1), ('b', 'cache', 0, 5), ('b', 'gh', 0, 4), ('c', 'pausa', 0, 1)]
        self.assertEqual(g.pivo(linhas), [('b', 0, 5, 0, 4, 9), ('a', 3, 0, 0, 0, 3), ('c', 0, 0, 1, 0, 1)])


class GithubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Mock()
        self.response(200, {'ok': 1})

    def response(self, status, body, remaining=900):
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout=(
            f'HTTP/2.0 {status} OK\nETag: "v1"\nX-RateLimit-Remaining: {remaining}\n'
            'X-RateLimit-Reset: 3000\n\n' + (json.dumps(body) if body is not None else '')))

    def get(self, now=1000, **kw):
        return g.get('repos/o/r/pulls/1', base_dados=self.tmp.name, agora=now,
                     rodar=self.run, token=kw.pop('token', 'test-token'), **kw)

    def test_banco_corrompido_nao_derruba_leitura_feita(self):
        """'database disk image is malformed' ao gravar o cache não pode virar erro do `mover Done`."""
        import sqlite3
        from unittest import mock

        class Quebrado:
            def __init__(self, con):
                self.con = con

            def execute(self, sql, *a):
                if sql.lstrip().startswith(('INSERT OR REPLACE INTO cache', 'DELETE')):
                    raise sqlite3.DatabaseError('database disk image is malformed')
                return self.con.execute(sql, *a)

            def __getattr__(self, nome):
                return getattr(self.con, nome)

        with mock.patch.object(g, '_banco_ou_memoria', lambda b=None: Quebrado(g._banco(b))):
            self.assertEqual(self.get(), {'ok': 1})

    def test_cache_etag_and_304(self):
        self.assertEqual(self.get(), {'ok': 1})
        self.get(1059)
        self.assertEqual(self.run.call_count, 1)
        self.response(304, None)
        self.assertEqual(self.get(1060), {'ok': 1})
        self.assertIn('If-None-Match: "v1"', self.run.call_args.args[0])
        self.assertEqual(sum(r[3] for r in g.resumo(self.tmp.name, 1060)), 3)

    def test_low_quota_pauses_until_reset_and_incident_deduplicates(self):
        self.response(200, {}, 499)
        self.get()
        self.get(1060, urgente=True)
        self.assertEqual(len(incidentes.listar(base_dados=self.tmp.name)), 1)
        with self.assertRaises(g.CotaBaixa):
            self.get(1120)
        self.assertEqual(self.run.call_count, 2)
        self.get(3001)
        self.assertEqual(self.run.call_count, 3)

    def test_auth_isolation_and_no_secrets_in_telemetry(self):
        self.get(consumidor='travadas')
        self.get(token='other-token', consumidor='automerge')
        self.assertEqual(self.run.call_count, 2)
        self.assertNotIn('test-token', repr(g.resumo(self.tmp.name, 1000)))
        self.assertEqual({r[0] for r in g.resumo(self.tmp.name, 1000)}, {'travadas', 'automerge'})

    def test_error_not_cached_and_not_leaked(self):
        self.response(403, {'message': 'private'})
        self.run.return_value.returncode = 1
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403') as caught:
            self.get()
        self.assertNotIn('private', str(caught.exception))
        self.response(200, {})
        self.get()
        self.assertEqual(self.run.call_count, 2)

    def test_rejects_external_url(self):
        with self.assertRaises(ValueError):
            g.get('https://evil.test/repos/o/r', token='test')
        self.run.assert_not_called()
