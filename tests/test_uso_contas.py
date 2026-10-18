import datetime as dt
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from hub import painel, uso_contas as U
from tests.test_painel import cockpit_neutro

AGORA = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)


def lin(horas_atras, conta=None, nome="t", modelo="claude-sonnet-4", ok=True, limite=False, custo=1.0, dur=600):
    ini = AGORA - dt.timedelta(hours=horas_atras)
    r = {"inicio": ini.isoformat(), "nome": nome, "modelo": modelo, "ok": ok, "limite": limite, "custo_usd": custo,
         "duracao_s": dur, "tokens": {"entrada": 10, "saida": 5, "cache_leitura": 100, "cache_escrita": 1}}
    if conta:
        r["conta"] = conta
    return r


USOS = [lin(1), lin(2, "1", nome="b"), lin(10, "2", modelo="claude-opus-4"), lin(30, "2", ok=False, limite=True),
        lin(100, "3"), lin(4, "3", nome="b", custo=2.0), lin(3, "4", nome="aio")]


class ResumoTest(unittest.TestCase):
    def test_soma_por_conta_e_janela(self):
        r = U.resumo(USOS, AGORA)
        self.assertEqual(set(r), {"1", "2", "3", "4"})  # chave para todas; o painel filtra as com login
        j1 = r["1"]["janelas"]  # linha sem `conta` é da conta 1
        self.assertEqual((j1["5h"]["execucoes"], j1["24h"]["execucoes"], j1["7d"]["execucoes"]), (2, 2, 2))
        self.assertEqual(j1["5h"]["minutos"], 20)
        self.assertEqual(j1["5h"]["tokens"], 232)
        j2 = r["2"]["janelas"]
        self.assertEqual((j2["5h"]["execucoes"], j2["24h"]["execucoes"], j2["7d"]["execucoes"]), (0, 1, 2))
        self.assertEqual((r["2"]["falhas"], r["2"]["limites"]), (1, 1))
        self.assertEqual(r["3"]["janelas"]["7d"]["custo"], 3.0)
        self.assertEqual(r["3"]["janelas"]["5h"]["custo"], 2.0)
        self.assertEqual(r["2"]["modelo"] in ("opus", "sonnet"), True)
        self.assertEqual(r["3"]["tarefas"][0]["nome"], "b")  # a que mais gasta primeiro
        self.assertEqual((r["4"]["janelas"]["5h"]["execucoes"], r["4"]["tarefas"][0]["nome"]), (1, "aio"))

    def test_horas(self):
        h = U.horas(USOS, AGORA)
        self.assertEqual(len(h["1"]), 48)
        self.assertEqual(h["1"][-2], 10.0)  # execução das 11:00 cai na penúltima barra (a última é a hora corrente)


def http_erro(codigo):
    return urllib.error.HTTPError("u", codigo, "x", {}, io.BytesIO(b""))


def cred(home, n, expira_ms):
    d = Path(home) / (".claude" if n == "1" else f".claude-{n}")
    d.mkdir(exist_ok=True)
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-SEGREDO",
                                                                         "expiresAt": expira_ms}}))


class ColetarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.fut = int((AGORA + dt.timedelta(hours=5)).timestamp() * 1000)
        for n in "123":
            cred(self.home, n, self.fut)
        self.orch = Path(self.tmp.name) / "orch.json"  # nunca ler /home/usuario de verdade
        p = mock.patch.dict(os.environ, {"USO_ORQ_CREDENCIAIS": str(self.orch)})
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_401_cai_em_estimativa(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_erro(401)):
            r = U.coletar(self.home, AGORA)
        for n in "123":
            self.assertEqual(r["contas"][n], {"cinco_horas": {"pct": None, "reset": None},
                                              "semana": {"pct": None, "reset": None}, "fonte": "estimativa"})
        self.assertNotIn("SEGREDO", json.dumps(r))

    def test_oauth_ok_e_token_expirado_sem_rede(self):
        cred_exp = self.home / ".claude-2" / ".credentials.json"
        cred_exp.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": 1}}))
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(
            {"five_hour": {"utilization": 12.5, "resets_at": "2026-10-08T15:00:00+00:00"}, "seven_day": None}).encode()
        with mock.patch("urllib.request.urlopen", return_value=resp) as m:
            r = U.coletar(self.home, AGORA)
        self.assertEqual(m.call_count, 2)  # a conta 2 expirada nem sai para a rede (sem renovar token)
        self.assertEqual(r["contas"]["1"]["fonte"], "oauth")
        self.assertEqual(r["contas"]["1"]["cinco_horas"]["pct"], 12.5)
        self.assertIsNone(r["contas"]["1"]["semana"]["pct"])
        self.assertEqual(r["contas"]["2"]["fonte"], "estimativa")

    def _cred_orq(self, expira_ms):
        self.orch.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-ORQSEGREDO", "expiresAt": expira_ms}}))

    def test_orquestrador_oauth_fora_de_contas_sem_token(self):
        self._cred_orq(self.fut)
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(
            {"five_hour": {"utilization": 7, "resets_at": "x"}, "seven_day": {"utilization": 30, "resets_at": "y"}}).encode()
        with mock.patch("urllib.request.urlopen", return_value=resp):
            r = U.coletar(self.home, AGORA)
        self.assertEqual((r["orquestrador"]["fonte"], r["orquestrador"]["cinco_horas"]["pct"], r["orquestrador"]["semana"]["pct"]),
                         ("oauth", 7.0, 30.0))
        self.assertEqual(set(r["contas"]), {"1", "2", "3"})
        self.assertNotIn("SEGREDO", json.dumps(r))

    def test_so_coleta_contas_com_login_e_a_4_entra_com_login(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_erro(401)) as m:
            self.assertEqual(set(U.coletar(self.home, AGORA)["contas"]), {"1", "2", "3"})
            tres = m.call_count
            (self.home / ".claude-4").mkdir()  # pasta sem login: fica de fora
            self.assertEqual(set(U.coletar(self.home, AGORA)["contas"]), {"1", "2", "3"})
            cred(self.home, "4", self.fut)
            self.assertEqual(set(U.coletar(self.home, AGORA)["contas"]), {"1", "2", "3", "4"})
        self.assertEqual(m.call_count, 3 * tres + 1)

    def test_orquestrador_ausente_ou_vencido_sem_rede(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_erro(401)) as m:
            ausente = U.coletar(self.home, AGORA)["orquestrador"]
            n3 = m.call_count
            self._cred_orq(1)
            vencido = U.coletar(self.home, AGORA)["orquestrador"]
        self.assertEqual((ausente, vencido), (U.VAZIA, U.VAZIA))
        self.assertEqual(m.call_count, 2 * n3)  # só as 3 contas saem para a rede

    def test_semana_e_sessao_pelo_limits(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps({"five_hour": None, "seven_day": None, "limits": [
            {"kind": "session", "percent": 11, "resets_at": "2026-10-09T02:00:00+00:00"},
            {"kind": "weekly_scoped", "percent": 0, "resets_at": "x"},
            {"kind": "weekly_all", "percent": 42, "resets_at": "2026-10-10T02:00:00+00:00"}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=resp):
            c = U.coletar(self.home, AGORA)["contas"]["1"]
        self.assertEqual((c["cinco_horas"]["pct"], c["semana"]["pct"], c["fonte"]), (11.0, 42.0, "oauth"))
        self.assertEqual(c["semana"]["reset"], "2026-10-10T02:00:00+00:00")

    def test_excecao_http_de_uma_conta_nao_derruba(self):
        import http.client
        with mock.patch("urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"")):
            self.assertEqual(U.coletar(self.home, AGORA)["contas"]["1"]["fonte"], "estimativa")

    def test_main_grava_atomico_sem_token(self):
        alvo = Path(self.tmp.name) / "alvo-de-root"
        alvo.write_text("intocado", encoding="utf-8")
        os.chmod(alvo, 0o600)
        (Path(self.tmp.name) / "claude").mkdir(exist_ok=True)
        (Path(self.tmp.name) / "claude" / ".uso-contas.json.tmp").symlink_to(alvo)  # plantado pelo usuário claude
        with mock.patch("urllib.request.urlopen", side_effect=http_erro(401)), \
                mock.patch.dict(os.environ, {"HUB_DATA": self.tmp.name, "USO_CONTAS_HOME": str(self.home)}):
            U.main()
        arq = Path(self.tmp.name) / "claude" / "uso-contas.json"
        self.assertEqual(set(json.loads(arq.read_text())["contas"]), {"1", "2", "3"})
        self.assertEqual(json.loads(arq.read_text())["orquestrador"], U.VAZIA)
        self.assertNotIn("SEGREDO", arq.read_text())
        self.assertEqual(oct(arq.stat().st_mode & 0o777), "0o644")
        self.assertEqual(alvo.read_text(), "intocado")
        self.assertEqual(alvo.stat().st_mode & 0o777, 0o600)
        self.assertEqual([x.name for x in arq.parent.iterdir() if x.name.startswith(".uso-contas.") and not x.is_symlink()], [])


class PainelUsoTest(unittest.TestCase):
    def setUp(self):
        cockpit_neutro(self)
        h = tempfile.TemporaryDirectory()
        self.addCleanup(h.cleanup)
        self.home = Path(h.name)
        for n in "23":
            cred(self.home, n, 1)
        p = mock.patch.dict(os.environ, {"CLAUDE_HOME": h.name})
        p.start()
        self.addCleanup(p.stop)

    def test_conta_4_aparece_com_login(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"HUB_DATA": t}):
            (Path(t) / "claude").mkdir()
            # o gráfico de 48 h é do relógio de verdade: a execução tem que ser de 1 h atrás de agora, não do AGORA fixo
            r = {**lin(1, "4"), "inicio": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()}
            (Path(t) / "claude" / "uso.jsonl").write_text(json.dumps(r))
            sem = painel.rota("/uso")[1]
            cred(self.home, "4", 1)
            com = painel.rota("/uso")[1]
        self.assertEqual(sem.count('class="bloco uso-conta'), 4)  # 3 contas + orquestrador
        self.assertNotIn("Conta 4", sem)
        self.assertEqual(com.count('class="bloco uso-conta'), 5)
        self.assertIn('</i>Conta 4</span>', com)  # legenda do gráfico de 48 h

    def test_uso_tem_4_blocos_sem_segredo(self):
        with tempfile.TemporaryDirectory() as t:
            c = Path(t) / "claude"
            c.mkdir()
            (c / "uso.jsonl").write_text("\n".join(json.dumps(lin(h, n)) for h, n in ((1, None), (2, "2"), (3, "3"))))
            (c / "uso-contas.json").write_text(json.dumps({"em": AGORA.isoformat(), "contas": {
                "1": {"cinco_horas": {"pct": 40.0, "reset": "2026-10-08T15:00:00+00:00"},
                      "semana": {"pct": None, "reset": None}, "fonte": "oauth"}},
                "orquestrador": {"cinco_horas": {"pct": 25.0, "reset": None}, "semana": {"pct": 60.0, "reset": None},
                                 "fonte": "oauth"}}))
            (c / "pausa-2.json").write_text(json.dumps({"ate": "2999-01-01T00:00:00+00:00"}))
            with mock.patch.dict(os.environ, {"HUB_DATA": t}):
                status, corpo = painel.rota("/uso")
        self.assertEqual(status, 200)
        self.assertEqual(corpo.count('class="bloco uso-conta'), 4)
        self.assertIn("Orquestrador", corpo)
        self.assertIn("sem leitura", corpo)
        self.assertIn("alerta-faixa", corpo)
        self.assertIn("<svg", corpo)
        self.assertIn('href="/uso"', corpo)
        for proibido in ("access_token", "refresh_token", "bearer", "sk-ant"):  # "tokens" de consumo é legítimo
            self.assertNotIn(proibido, corpo.lower())


if __name__ == "__main__":
    unittest.main()
