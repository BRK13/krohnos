"""tokens do hub. Tipo de execução no uso.jsonl, aba Tokens, MCP e subagentes por tarefa, retomada com
resumo de estado e voltas da espera sem IA (ou no modelo barato). Nada fala com GitHub, Linear nem a CLI."""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import agenda, esperas, linear, painel, skills as S, tokens as TK
from tests.test_esperas import PR, SHA, T0, GitHubFalso, ci

UTC = dt.timezone.utc
AGORA = dt.datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def reg(nome, custo, horas_atras=1, modelo="opus", tipo=None, lido=100, escrita=10, dur=300):
    r = {"inicio": (AGORA - dt.timedelta(hours=horas_atras)).isoformat(), "origem": "tarefa", "nome": nome,
         "modelo": modelo, "custo_usd": custo, "duracao_s": dur,
         "tokens": {"entrada": 1, "saida": 5, "cache_leitura": lido, "cache_escrita": escrita}}
    if tipo:
        r["tipo"] = tipo
    return r


class TipoTest(unittest.TestCase):
    def test_motivos_da_fila(self):
        casos = {"voltou da espera do CI (x)": "espera_ci", "volta: conferência": "conferencia",
                 "volta: promessa sem gancho": "promessa", "volta: fala durante a execução": "fala_durante",
                 "volta: conferência ou fala nova": "conferencia", "voltou de Done sem recibo": "recibo",
                 "merge mecânico (CI verde, revisão limpa)": "merge", "proposta executada": "proposta",
                 "voltou para a fila: limite de uso até 10:00": "limite",
                 "voltou para a fila: interrompida por sinal (código 143)": "interrompida",
                 "agenda 16/10 08:00": "agendada", "manual": "manual", None: "agendada", "Debugger reativo": "debugger"}
        for motivo, tipo in casos.items():
            self.assertEqual(TK.tipo_execucao(motivo), tipo, motivo)

    def test_linear_primeira_fala_e_deploy(self):
        self.assertEqual(TK.tipo_execucao("linear KRN-1", {"comentarios": ["Fulano: oi"]}), "primeira")
        self.assertEqual(TK.tipo_execucao("linear KRN-1", {"comentarios": ["Claude: feito", "Fulano: e?"]}), "fala")
        self.assertEqual(TK.tipo_execucao("voltou da espera do CI (x)", {"roteado_por": "retomada de deploy"}),
                         "espera_deploy")

    def test_volta_do_concluir_diz_o_motivo(self):
        self.assertEqual(linear.por_que_volta({"comentarios": ["Conferência automática (faça agora): faltou X"]}),
                         "conferência")
        self.assertEqual(linear.por_que_volta({"comentarios": ["Fulano (durante a execução anterior): e?"]}),
                         "fala durante a execução")
        self.assertEqual(linear.por_que_volta({"promessas": 1, "comentarios": ["Krohnos: mudou"]}), "promessa sem gancho")

    def test_executar_grava_o_tipo(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"HUB_DATA": d}), \
                mock.patch.object(agenda, "montar_prompt", lambda t, b=None, **k: "p"):
            t = agenda.T.Tarefa("x", "manual", 2, "sonnet", 60, [], False, "x", "p")
            t.motivo = "agenda 16/10 08:00"
            r = agenda.executar(t, base_dados=d, chamar=lambda *a, **k: (0, {"result": "ok"}, ""), saida_dir=d)
            self.assertEqual(r["tipo"], "agendada")


class ResumoTest(unittest.TestCase):
    def usos(self):
        return [reg("linear-krn-1", 90, tipo="espera_ci", dur=30), reg("linear-krn-2", 6, modelo="sonnet"),
                reg("linear-krn-2", 4, modelo="sonnet"), reg("conversa", 0, modelo="codex"),
                reg("linear-krn-3", 1000, horas_atras=30)]  # fora das 24 h

    def test_por_tipo_modelo_top_e_alerta(self):
        r = TK.resumo(self.usos(), AGORA)
        self.assertEqual(r["total"]["execucoes"], 4)
        self.assertEqual(r["total"]["custo_usd"], 100)
        self.assertEqual(r["por_tipo"]["espera_ci"]["curtas"], 1)
        self.assertEqual(r["por_tipo"]["sem_tipo"]["execucoes"], 3)
        self.assertEqual(r["por_modelo"]["sonnet"]["custo_usd"], 10)
        self.assertEqual([x["issue"] for x in r["top"]][:2], ["KRN-1", "KRN-2"])
        self.assertEqual([x["issue"] for x in r["alertas"]], ["KRN-1", "KRN-2"])  # 90% e 10% (> 5%)

    def test_antes_depois_por_dia(self):
        corte = AGORA - dt.timedelta(hours=12)
        usos = [reg("linear-krn-1", 10, horas_atras=20, lido=1_000_000), reg("linear-krn-2", 10, horas_atras=2, lido=200_000)]
        antes, depois = TK.antes_depois(usos, corte, horas=24, agora=AGORA)
        self.assertEqual((antes["execucoes"], depois["execucoes"], depois["horas"]), (1, 1, 12))
        tab = TK.tabela(antes, depois)
        self.assertIn("| Execuções por dia (issues) | 1 | 2 | < 300 |", tab)
        self.assertIn("| Cache lido por execução (mediana) | 1.000.000 | 200.000 | < 300 mil |", tab)

    def test_aba_tokens_no_painel(self):
        with mock.patch.object(painel.C, "agora", lambda: AGORA):
            h = painel._tokens(self.usos(), AGORA)
        self.assertIn("Acima de 5% do custo do dia: KRN-1", h)
        self.assertIn("Top 10 issues mais caras", h)
        self.assertIn("espera_ci", h)
        self.assertIn('href="/uso?aba=tokens"', painel.uso("contas"))


class McpESubagentesTest(unittest.TestCase):
    def test_mcp_so_sob_demanda(self):
        self.assertEqual(S.mcp_da_tarefa("Corrigir o teto do disjuntor"), [])
        self.assertEqual(S.mcp_da_tarefa("Trilha do vídeo da fsite06"), ["earshot"])
        self.assertEqual(S.mcp_da_tarefa("Engenharia reversa do app", ["openseo"]), ["openseo", "rea"])
        self.assertIn("openseo", S.mcp_da_tarefa("Artigo novo no blog"))

    def test_mcp_da_issue_ignora_texto_do_claude_e_le_label(self):
        t = linear.tarefa({"identifier": "KRN-9", "titulo": "Ajuste no hub", "url": "u", "descricao": "d", "repos": [],
                           "modelo": "sonnet", "mcp": ["rea"],
                           "comentarios": ["Claude: gerei o vídeo e a trilha", "Fulano: ok"]})
        self.assertEqual(agenda.mcp_da_tarefa(t), ["rea"])

    def test_mcp_json_pega_definicao_das_contas(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as pasta, \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}):
            Path(home, ".claude-2").mkdir()
            Path(home, ".claude-2", ".claude.json").write_text(json.dumps({"mcpServers": {"rea": {"command": "rea"}}}))
            arq, ligados = S.mcp_json(pasta, ["rea", "earshot", "openseo"], home=home)
            servidores = json.loads(Path(arq).read_text())["mcpServers"]
        self.assertEqual(ligados, ["rea", "openseo"])  # earshot não está declarado em conta nenhuma
        self.assertEqual(servidores["rea"], {"command": "rea"})

    def test_chamar_claude_liga_strict_mcp(self):
        cmds = []
        with mock.patch.object(agenda, "_rodar_cli", lambda cmd, *a, **k: cmds.append(cmd) or ("ok", 0, "", "")):
            agenda.chamar_claude("p", "sonnet", 60, "/tmp", {}, skills=[], mcp=[])
            agenda.chamar_claude("p", "sonnet", 60, "/tmp", {})
        self.assertIn("--strict-mcp-config", cmds[0])
        self.assertTrue(cmds[0][cmds[0].index("--mcp-config") + 1].endswith("mcp.json"))
        self.assertNotIn("--strict-mcp-config", cmds[1])  # sem mcp: os da conta, como antes

    def test_plugin_traz_revisor_e_explorador_em_haiku(self):
        with tempfile.TemporaryDirectory() as d:
            plugin = S.montar(d, [])["plugin"]
            for nome in ("revisor", "explorador"):
                texto = Path(plugin, "agents", f"{nome}.md").read_text(encoding="utf-8")
                self.assertIn(f"name: {nome}\n", texto)
                self.assertIn("model: haiku\n", texto)

    def test_prompt_diz_mcp_e_subagentes(self):
        t = linear.tarefa({"identifier": "KRN-9", "titulo": "Ajuste", "url": "u", "descricao": "d", "repos": [],
                           "modelo": "sonnet", "comentarios": []})
        texto = "\n".join(agenda._secao_skills(t))
        self.assertIn("MCP desta execução: nenhum", texto)
        self.assertIn("precisa mcp:<nome>", texto)
        self.assertIn("tarefa:revisor", texto)


class RetomadaTest(unittest.TestCase):
    def test_conversa_longa_cabe_no_limite_e_guarda_as_ultimas_falas(self):
        c = ([f"Linear: aviso {i} " + "a" * 900 for i in range(5)]
             + [f"Fulano: fala {i} " + "b" * 1500 for i in range(5)]
             + [f"Claude: resposta {i}\n" + "c" * 3000 for i in range(4)] + ["Claude: estado final\n" + "d" * 6000])
        saida, resumiu = linear.conversa_enxuta(c)
        self.assertTrue(resumiu)
        self.assertLess(sum(map(len, saida)), 13_000)  # de ~35 mil; as 3 últimas falas nunca são cortadas
        for i in (7, 8, 9):  # as 3 últimas falas inteiras
            self.assertEqual(saida[i], c[i])
        self.assertTrue(saida[5].endswith("(cortado; inteiro no Linear)"))
        self.assertTrue(saida[-1].startswith("Claude: estado final"))

    def test_prompt_lista_os_prs_citados(self):
        c = ["Claude: abri https://github.com/acme/x/pull/1\n" + "x" * linear.CONVERSA_MAX, "Fulano: e?",
             "Claude: PR https://github.com/acme/x/pull/2 no CI", "Claude: fim"]
        texto = linear.tarefa({"identifier": "KRN-9", "titulo": "t", "url": "u", "descricao": "d", "repos": [],
                               "modelo": "sonnet", "comentarios": c}).prompt
        self.assertIn("PRs citados na conversa: https://github.com/acme/x/pull/1, https://github.com/acme/x/pull/2", texto)


class VoltaSemIaTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        self.eventos = []

        def reabrir(ident, fala):
            return linear.tarefa({"tipo": "issue", "id": "u", "identifier": ident, "titulo": "t", "descricao": "",
                                  "url": "x", "modelo": "opus", "repos": [], "comentarios": [fala], "estados": {}})
        item = {"tipo": "issue", "id": "uid", "identifier": "KRN-164", "merge": False, "estados": {"Done": "d"}}
        for p in (mock.patch.dict(os.environ, {"HUB_DATA": tmp.name}),
                  mock.patch("hub.agenda.em_loop", lambda *a, **k: None),
                  mock.patch.object(linear, "reabrir", reabrir),
                  mock.patch.object(linear, "item_por_ident", lambda i: item),
                  mock.patch.object(linear, "encerrar_sem_ia", lambda it, est, txt: self.eventos.append((est, txt)))):
            p.start()
            self.addCleanup(p.stop)

    def _volta(self, gh):
        esperas.gravar("krn-164", PR, 6, chamar=GitHubFalso(), base_dados=self.dados, agora=T0)
        agenda.voltar_das_esperas({}, self.dados, chamar=gh, agora=T0 + dt.timedelta(minutes=45))
        return agenda.fila(self.dados)[0]["linear"]

    def test_verde_sem_revisao_vai_no_sonnet(self):
        item = self._volta(GitHubFalso(ci("success")))
        self.assertEqual((item["modelo"], item["roteado_por"]), ("sonnet", "retomada mecânica"))

    def test_vermelho_fica_no_modelo_da_issue(self):
        self.assertEqual(self._volta(GitHubFalso(ci("failure")))["modelo"], "opus")

    def _no_ar_sem_deploy(self, get, site):
        pr = PR.replace("clienteh", "sem-deploy")  # clienteh lê /api/versao do ClienteJ
        esperas.gravar("krn-164", pr, 6, chamar=GitHubFalso(), base_dados=self.dados, agora=T0, no_ar="")
        gh = GitHubFalso(pr={"state": "closed", "merged": True, "merge_commit_sha": SHA, "base": {"ref": "main"},
                             "head": {"sha": SHA}})
        with mock.patch("hub.recibo._site", return_value=site), mock.patch.object(esperas, "_get_texto", get):
            return agenda.voltar_das_esperas({}, self.dados, chamar=gh, agora=T0 + dt.timedelta(minutes=15))

    def test_deploy_sem_conferencia_com_site_volta_com_ia(self):
        # 200 da página antiga não prova o deploy (revisão do Codex no #317): a IA confere
        self.assertEqual(self._no_ar_sem_deploy(lambda u: "<html>", {"dominio": "exemplo.com", "repo": {}}),
                         ["linear-krn-164"])
        self.assertEqual(self.eventos, [])

    def test_deploy_sem_conferencia_e_sem_site_fecha_sem_ia(self):
        # INC-76: o fechamento consulta o recibo da issue (Linear + GitHub); aqui o recibo está em dia
        with mock.patch.object(linear, "gql", lambda *a, **k: {"issue": {}}), \
                mock.patch("hub.portao_done.avaliar", lambda *a, **k: {"ok": True, "faltas": []}):
            self._fecha_sem_site()

    def _fecha_sem_site(self):
        self.assertEqual(self._no_ar_sem_deploy(lambda u: "<html>", None), [])
        est, txt = self.eventos[0]
        self.assertEqual(est, "Done")
        self.assertIn("Fechado pelo hub, sem IA", txt)
        self.assertEqual(agenda.fila(self.dados), [])

if __name__ == "__main__":
    unittest.main()
