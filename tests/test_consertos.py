import datetime as dt
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from hub import consertos, resumo

UTC = dt.timezone.utc
AGORA = dt.datetime(2026, 10, 8, 13, 0, tzinfo=UTC)


class ConsertosTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        for p in ("claude", "rotinas", "vps", "verificador/fsite06"):
            (self.d / p).mkdir(parents=True)

    def test_notas_do_bot_falha_a_consertar_e_nota_com_pendencia_no_resumo(self):
        arq = self.d / "notas-tecnicas.jsonl"
        self.escrever("notas-tecnicas.jsonl", [
            {"em": "2026-10-08T11:00:00.000Z", "tipo": "falha", "onde": "Secretaria", "texto": "deploy não subiu"},
            {"em": "2026-10-08T11:05:00.000Z", "tipo": "nota", "onde": "Secretaria", "texto": "Feito. Pendência: o Fulano precisa aprovar o texto."},
            {"em": "2026-10-08T11:10:00.000Z", "tipo": "nota", "onde": "Secretaria", "texto": "PR mergeado, suíte 172/172."},
            {"em": "2026-10-07T11:00:00.000Z", "tipo": "falha", "onde": "velha", "texto": "antiga"}])
        with open(arq, "a") as fh:
            fh.write("lixo\n" + json.dumps({"em": "2026-10-08T11:20:00Z", "tipo": "nota", "onde": "x", "texto": 5}) + "\n")
        with unittest.mock.patch.dict("os.environ", {"NOTAS_TECNICAS_ARQ": str(arq)}):
            n = consertos.novidades(dt.datetime(2026, 10, 8, 10, 0, tzinfo=UTC), self.d, com_gh=False, agora=AGORA)
            self.assertEqual([f["texto"] for f in n["bot_falhas"]], ["deploy não subiu"])
            self.assertEqual(len(n["bot_notas_pendentes"]), 1)
            self.assertIn("aprovar o texto", n["bot_notas_pendentes"][0]["texto"])
            _, precisa = consertos.blocos_resumo(self.d, AGORA)
            self.assertEqual(len([l for l in precisa if "nota do bot" in l]), 1)
        with unittest.mock.patch.dict("os.environ", {"NOTAS_TECNICAS_ARQ": str(self.d / "nao-existe")}):
            self.assertEqual(consertos.notas_bot(AGORA - dt.timedelta(days=1), AGORA), ([], []))

    def escrever(self, rel, linhas):
        (self.d / rel).write_text("".join(json.dumps(l) + "\n" for l in linhas), encoding="utf-8")

    def test_novidades_junta_o_que_quebrou_desde_a_ultima_rodada(self):
        self.escrever("claude/uso.jsonl", [
            {"nome": "velha", "ok": False, "inicio": "2026-10-08T05:00:00+00:00", "fim": "2026-10-08T05:01:00+00:00"},
            {"nome": "seo-semanal", "ok": False, "inicio": "2026-10-08T11:00:00+00:00", "fim": "2026-10-08T11:30:00+00:00", "erro": "x"},
            {"nome": "boa", "ok": True, "inicio": "2026-10-08T12:00:00+00:00", "fim": "2026-10-08T12:01:00+00:00"}])
        self.escrever("rotinas/falhas.jsonl", [{"em": "2026-10-08T12:03:25+00:00", "nome": "coleta", "codigo": 1, "fim": "..."}])
        self.escrever("vps/consertos.jsonl", [{"em": "2026-10-08T12:10:00+00:00", "acao": "reiniciar-servico", "codigo": 2}])
        (self.d / "latest.json").write_text(json.dumps({"at": "x", "projects": [
            {"id": "asite01", "checks": {"status": {"ok": True}, "clarity": {"ok": False, "error": "quota"}}},
            {"id": "site-desativado", "checks": {"gsc": {"ok": False}}}]}))
        (self.d / "verificador/fsite06/latest.json").write_text(json.dumps({"pioras": [{"n": 3, "item": "canonical"}]}))
        n = consertos.novidades(dt.datetime(2026, 10, 8, 10, 0, tzinfo=UTC), self.d, com_gh=False, agora=AGORA)
        self.assertEqual([t["nome"] for t in n["tarefas_falharam"]], ["seo-semanal"])
        self.assertEqual(n["rotinas_falharam"][0]["nome"], "coleta")
        self.assertEqual(n["root_falhou"][0]["acao"], "reiniciar-servico")
        self.assertEqual([(c["site"], c["check"]) for c in n["coleta_checks_falhando"]], [("asite01", "clarity")])
        self.assertEqual(n["verificador_piorou"][0]["site"], "fsite06")

    def test_marcar_e_desde_padrao(self):
        consertos.marcar(self.d, AGORA - dt.timedelta(hours=1))
        n = consertos.novidades(None, self.d, com_gh=False, agora=AGORA)
        self.assertEqual(n["desde"], "2026-10-08T12:00:00+00:00")

    def test_ci_vermelho_pega_ultimo_run_de_cada_workflow(self):
        def gh(args):
            if args[0] == "api":
                return {"b": "main"}
            return [{"workflowName": "CI", "conclusion": "failure", "url": "u1", "createdAt": "2"},
                    {"workflowName": "CI", "conclusion": "success", "url": "u0", "createdAt": "1"},
                    {"workflowName": "Lint", "conclusion": "success", "url": "u2", "createdAt": "2"}]
        self.assertEqual([c["url"] for c in consertos.ci_vermelho(("x",), gh)], ["u1"])

    def test_registrar_e_resumo(self):
        consertos.registrar("coleta falhou (código 1)", "Clarity da asite01 sem cota", "coleta tolera falha isolada",
                            "consertado", "rerodar-rotina coleta: código 0", base_dados=self.d, agora=AGORA)
        consertos.registrar("backup do CRM nunca rodou", "falta cron", "Pedido N tipo vps", "precisa_fulano",
                            pedido="12", base_dados=self.d, agora=AGORA)
        with self.assertRaises(ValueError):
            consertos.registrar("x", "", "", "talvez", base_dados=self.d)
        a, b = consertos.blocos_resumo(self.d, AGORA)
        self.assertIn("coleta tolera falha isolada", "\n".join(a))
        self.assertIn("Pedido/proposta 12", "\n".join(b))
        texto = resumo.dados(AGORA, self.d, com_gh=False)
        self.assertIn("O que quebrou e já foi consertado", texto)
        self.assertIn("O que precisa do Ana", texto)


if __name__ == "__main__":
    unittest.main()
