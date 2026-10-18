"""GSC pelo OAuth do Fulano quando a conta de serviço não tem acesso, com respostas fixas do Google."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from hub import bi_historico, google

CLIENTEI, CLIENTEE = "sc-domain:clientef-com.example", "sc-domain:clienteeemoraesadv-com.example"
OAUTH = {"client_id": "cid.apps.googleusercontent.com", "client_secret": "segredo-x", "refresh_token": "1//refresh-y"}
LINHAS = {CLIENTEI: [{"keys": ["2026-10-01"], "clicks": 40, "impressions": 900, "ctr": 0.044, "position": 9.1}],
          CLIENTEE: [{"keys": ["2026-10-01"], "clicks": 3, "impressions": 120, "ctr": 0.025, "position": 14.2}]}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class GoogleFalso:
    """A conta de serviço vê só a ClienteI; o Fulano (OAuth) vê as duas. Registra quem chamou o quê."""

    def __init__(self, fulano_ve=(CLIENTEI, CLIENTEE), token_recusado=False, escopo="https://www.googleapis.com/auth/webmasters",
                 cota=False):
        self.fulano_ve, self.token_recusado, self.escopo, self.cota, self.chamadas = set(fulano_ve), token_recusado, escopo, cota, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        if url == "https://oauth2.googleapis.com/token":
            corpo = dict(x.split("=", 1) for x in req.data.decode().split("&"))
            self.chamadas.append(("token", corpo["grant_type"]))
            if self.token_recusado:
                raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)
            return _Resp(json.dumps({"access_token": "tk-fulano", "expires_in": 3599, "scope": self.escopo}).encode())
        quem = {"Bearer tk-sa": "sa", "Bearer tk-fulano": "fulano"}[req.get_header("Authorization")]
        prop = next(p for p in LINHAS if p.replace(":", "%3A") in url)
        self.chamadas.append((quem, prop))
        if self.cota:
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}'))
        if (quem == "sa" and prop != CLIENTEI) or (quem == "fulano" and prop not in self.fulano_ve):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        return _Resp(json.dumps({"rows": LINHAS[prop]}).encode())


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        # NIMBUS_HOME vazio: sem ele, como root na VPS, o _credencial caía no OAuth real de /etc/nimbus/so/credenciais.
        vazio = tempfile.TemporaryDirectory()
        self.addCleanup(vazio.cleanup)
        for p in (mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": self.d.name, "NIMBUS_HOME": vazio.name}),
                  mock.patch.object(google, "_tokens", {}), mock.patch.object(google, "_via_oauth", set()),
                  mock.patch.object(google, "token", return_value="tk-sa"),
                  mock.patch.object(google, "conta_servico", return_value={"client_email": "user7@example.com"})):
            p.start()
            self.addCleanup(p.stop)

    def oauth(self, conteudo=OAUTH):
        (Path(self.d.name) / google.OAUTH_ARQUIVO).write_text(json.dumps(conteudo), encoding="utf-8")

    def consultar(self, falso, prop):
        with mock.patch("urllib.request.urlopen", side_effect=falso):
            return bi_historico.consultar("gsc", prop, {"startDate": "2026-10-01", "endDate": "2026-10-01", "dimensions": ["date"]})


class FallbackTest(Base):
    def test_clientei_segue_pela_conta_de_servico_sem_tocar_no_oauth(self):
        self.oauth()
        g = GoogleFalso()
        self.assertEqual(self.consultar(g, CLIENTEI), LINHAS[CLIENTEI])
        self.assertEqual(g.chamadas, [("sa", CLIENTEI)])

    def test_clientee_cai_no_oauth_do_fulano(self):
        self.oauth()
        g = GoogleFalso()
        self.assertEqual(self.consultar(g, CLIENTEE), LINHAS[CLIENTEE])
        self.assertEqual(g.chamadas, [("sa", CLIENTEE), ("token", "refresh_token"), ("fulano", CLIENTEE)])

    def test_proxima_chamada_vai_direto_no_oauth_e_reusa_o_token(self):
        self.oauth()
        g = GoogleFalso()
        self.consultar(g, CLIENTEE)
        g.chamadas.clear()
        self.consultar(g, CLIENTEE)
        self.consultar(g, CLIENTEI)
        self.assertEqual(g.chamadas, [("fulano", CLIENTEE), ("sa", CLIENTEI)])

    def test_sem_arquivo_ou_padrao_vazio_do_systemd_fica_sem_acesso(self):
        for conteudo in (None, {}):
            with self.subTest(conteudo=conteudo):
                google._tokens.clear()
                if conteudo is not None:
                    self.oauth(conteudo)
                g = GoogleFalso()
                with self.assertRaises(google.SemAcesso) as c:
                    self.consultar(g, CLIENTEE)
                self.assertEqual(c.exception.email, "user7@example.com")
                self.assertEqual(g.chamadas, [("sa", CLIENTEE)])

    def test_fulano_tambem_sem_acesso_fica_sem_acesso(self):
        self.oauth()
        g = GoogleFalso(fulano_ve=())
        with self.assertRaises(google.SemAcesso):
            self.consultar(g, CLIENTEE)
        self.assertNotIn(CLIENTEE, google._via_oauth)

    def test_fulano_perde_o_acesso_volta_para_a_conta_de_servico(self):
        self.oauth()
        g = GoogleFalso()
        self.consultar(g, CLIENTEE)
        g.fulano_ve.clear()
        g.chamadas.clear()
        with self.assertRaises(google.SemAcesso):
            self.consultar(g, CLIENTEE)
        self.assertEqual(g.chamadas, [("fulano", CLIENTEE), ("sa", CLIENTEE), ("fulano", CLIENTEE)])
        self.assertNotIn(CLIENTEE, google._via_oauth)

    def test_refresh_recusado_nao_vaza_segredo_e_fica_sem_acesso(self):
        self.oauth()
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(google.SemAcesso) as c:
            self.consultar(GoogleFalso(token_recusado=True), CLIENTEE)
        texto = err.getvalue() + str(c.exception)
        self.assertIn("HTTP 400", err.getvalue())
        for segredo in OAUTH.values():
            self.assertNotIn(segredo, texto)

    def test_token_sem_escopo_webmasters_nao_e_usado(self):
        self.oauth()
        g = GoogleFalso(escopo="https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/gmail.readonly")
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(google.SemAcesso):
            self.consultar(g, CLIENTEE)
        self.assertIn("escopo webmasters", err.getvalue())
        self.assertNotIn(("fulano", CLIENTEE), g.chamadas)

    def test_recusa_do_refresh_fica_em_cache(self):
        self.oauth()
        g = GoogleFalso(token_recusado=True)
        with redirect_stderr(io.StringIO()):
            for _ in range(3):
                with self.assertRaises(google.SemAcesso):
                    self.consultar(g, CLIENTEE)
        self.assertEqual(sum(c[0] == "token" for c in g.chamadas), 1)

    def test_403_de_cota_nao_troca_de_credencial(self):
        self.oauth()
        g = GoogleFalso(cota=True)
        with self.assertRaises(urllib.error.HTTPError):
            self.consultar(g, CLIENTEI)
        self.assertEqual(g.chamadas, [("sa", CLIENTEI)])

    def test_ga4_nao_usa_o_oauth(self):
        self.oauth()
        erro = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with mock.patch.object(google, "token_oauth", side_effect=AssertionError("GA4 não usa o OAuth")), \
                mock.patch.object(google, "_chamar", side_effect=erro):
            with self.assertRaises(google.SemAcesso):
                google._api(google._ANALYTICS + "111111111:runReport", "111111111", {}, google.ESCOPO_GA4)


class SitemapEnvioTest(Base):
    URL = "https://clientee.example/sitemap.xml"

    def enviar(self, g):
        reqs = []

        def falso(req, timeout=None):
            reqs.append(req)
            return g(req, timeout)
        with mock.patch("urllib.request.urlopen", side_effect=falso):
            google.enviar_sitemap(CLIENTEE, self.URL)
        return reqs

    def test_put_na_url_certa_e_403_da_conta_de_servico_cai_no_oauth(self):
        self.oauth()
        g = GoogleFalso()
        g_resp = g.__call__

        def vazio(req, timeout=None):
            r = g_resp(req, timeout)
            return r if "oauth2" in req.full_url else _Resp(b"")
        reqs = self.enviar(vazio)
        esperado = google._base(CLIENTEE) + "/sitemaps/" + "https%3A%2F%2Fclientee.example%2Fsitemap.xml"
        self.assertEqual([(r.get_method(), r.full_url) for r in reqs if "googleapis.com/webmasters" in r.full_url or "searchconsole" in r.full_url],
                         [("PUT", esperado)] * 2)
        self.assertEqual(g.chamadas, [("sa", CLIENTEE), ("token", "refresh_token"), ("fulano", CLIENTEE)])

    def test_token_so_readonly_nao_serve_para_escrever(self):
        self.oauth()
        g = GoogleFalso(escopo="https://www.googleapis.com/auth/webmasters.readonly")
        with redirect_stderr(io.StringIO()), self.assertRaises(google.SemAcesso):
            self.enviar(g)
        self.assertNotIn(("fulano", CLIENTEE), g.chamadas)


class GravarOauthTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d))
        self.env, self.destino = self.d / "agente-fsite05.env", self.d / "credenciais" / google.OAUTH_ARQUIVO

    def test_copia_do_env_em_arquivo_600_e_so_regrava_se_mudou(self):
        self.env.write_text("# agente\nOUTRA=1\nexport GOOGLE_AGENDA_CLIENT_ID=cid.apps.googleusercontent.com\n"
                            "GOOGLE_AGENDA_CLIENT_SECRET='segredo-x'\nGOOGLE_AGENDA_REFRESH_TOKEN=\"1//refresh-y\"\n", encoding="utf-8")
        self.assertEqual(google.gravar_oauth(self.env, self.destino), "gravado")
        self.assertEqual(json.loads(self.destino.read_text(encoding="utf-8")), OAUTH)
        self.assertEqual(self.destino.stat().st_mode & 0o777, 0o600)
        self.assertEqual(google.gravar_oauth(self.env, self.destino), "igual")
        self.assertFalse(self.destino.with_name(self.destino.name + ".tmp").exists())

    def test_env_sem_as_tres_chaves_nao_grava(self):
        self.env.write_text("GOOGLE_AGENDA_CLIENT_ID=x\nGOOGLE_AGENDA_REFRESH_TOKEN=\n", encoding="utf-8")
        self.assertEqual(google.gravar_oauth(self.env, self.destino), "sem-oauth")
        self.assertFalse(self.destino.exists())

    def test_tirar_do_env_apaga_a_copia(self):
        self.env.write_text("".join(f"{k}=v\n" for k in google.OAUTH_ENV), encoding="utf-8")
        self.assertEqual(google.gravar_oauth(self.env, self.destino), "gravado")
        self.env.write_text("GOOGLE_AGENDA_CLIENT_ID=v\n", encoding="utf-8")
        self.assertEqual(google.gravar_oauth(self.env, self.destino), "removido")
        self.assertFalse(self.destino.exists())
        google.gravar_oauth(self.env, self.destino)
        self.env.unlink()
        self.assertEqual(google.gravar_oauth(self.env, self.destino), "sem-oauth")

    def test_comando_do_deploy_nao_imprime_segredo(self):
        import subprocess, sys
        self.env.write_text("GOOGLE_AGENDA_CLIENT_ID=cid.apps.googleusercontent.com\nGOOGLE_AGENDA_CLIENT_SECRET=segredo-x\n"
                            "GOOGLE_AGENDA_REFRESH_TOKEN=1//refresh-y\n", encoding="utf-8")
        for esperado in ("gravado", "igual"):
            r = subprocess.run([sys.executable, "-m", "hub.google", "gravar-oauth", str(self.env), str(self.destino)],
                               capture_output=True, text=True, cwd=Path(__file__).parent.parent, check=True)
            self.assertIn(esperado, r.stdout)
            for segredo in OAUTH.values():
                self.assertNotIn(segredo, r.stdout + r.stderr)
        self.assertEqual(json.loads(self.destino.read_text(encoding="utf-8")), OAUTH)


# DeployTest (a unit systemd e o atualizar.sh da nossa VPS) mora em tests/test_google_oauth_deploy.py; fora do
# snapshot público.


if __name__ == "__main__":
    unittest.main()
