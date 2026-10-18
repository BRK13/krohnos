import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import mcp_grupos, mcp_proxy, sites
from hub.mcp_grupos import ErroUso, projetos

PROPS = {  # propriedades do original
    "update_project_context": {"projectId", "updates", "site"},
    "create_project": {"name", "domain", "locationCode", "languageCode", "organizationId"},
    "whoami": set(),
    "research_project_website": {"projectId", "site", "website"},
    "save_project_website_setup": {"projectId", "site", "name", "domain", "overview", "competitors", "suggestedTopics",
                                   "suggestedKeywords"},
}

HOME = """<html><head><title>Instituto ClienteM</title><meta name="description" content="Fisioterapia e pilates">
<script>var x = "nao";</script></head><body><h1>Cuidado clientem</h1><p>Atendemos &amp; tratamos dores.</p>
<a href="/blog">Blog</a><a href="/servicos">Serviços</a><a href="https://corporativo.clientea-com.example/sobre#time">Sobre</a>
<a href="/contato">Contato</a><a href="/precos">Preços</a><a href="/a">A</a><a href="/b">B</a><a href="/c.pdf">PDF</a>
<a href="mailto:user11@example.com">mail</a><a href="#topo">topo</a><a href="https://concorrente-com-br.example/fsite05">Concorrente</a>
<a href="https://outro-com.example/">Outro</a></body></html>"""


def topico(nome, n=5):
    return {"name": nome, "prompts": [f"{nome} pergunta {i}" for i in range(n)]}


def resposta_ia(**extra):
    return json.dumps({"overview": "Clínica de fisioterapia. " + "x" * 5000,
                       "competitors": [{"name": "Concorrente", "domain": "www.concorrente-com-br.example", "notes": "também faz fsite05",
                                        "sourceUrl": "https://concorrente-com-br.example/fsite05"},
                                       {"name": "Sem prova", "domain": "semprova-com.example", "notes": "n", "sourceUrl": "https://semprova-com.example/"}],
                       "suggestedTopics": [topico("fisioterapia em casa"), topico("um dois tres quatro cinco seis"),
                                           topico("pilates", 4)] + [topico(f"tema {i}") for i in range(5)],
                       "suggestedKeywords": ["pilates clínico", "frase com seis palavras demais aqui"] + [f"kw {i}" for i in range(20)],
                       **extra})


class Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HUB_DATA": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def atualizar(self, *ops, site="psite13"):
        return mcp_proxy.executar("update_project_context", {"site": site, "updates": list(ops)})

    def criar(self, **args):
        return mcp_proxy.executar("create_project", {"name": "Clínica Nova", "domain": "https://www.clinicanova-com-br.example/x", **args})

    def test_schemas_iguais_ao_original(self):
        for nome, props in PROPS.items():
            self.assertEqual(set(mcp_proxy.FERRAMENTAS[nome]["inputSchema"]["properties"]), props, nome)
        up = mcp_proxy.FERRAMENTAS["update_project_context"]["inputSchema"]["properties"]["updates"]
        self.assertEqual((up["minItems"], up["maxItems"], len(up["items"]["anyOf"])), (1, 50, 9))
        self.assertEqual(mcp_proxy.FERRAMENTAS["create_project"]["inputSchema"]["required"], ["name"])

    def test_contexto_gravado_e_lido(self):
        r = self.atualizar({"section": "current_goal", "content": "  crescer  "},
                           {"customSection": "plano", "title": "Plano", "content": "v1"},
                           {"customSection": "plano", "content": "v2"},
                           {"addCompetitors": [{"domain": "https://www.zenklub-com-br.example/x", "name": "Zenklub"}]},
                           {"addKeyPages": [{"url": "www.psite13-com.example/blog#topo"}]},
                           {"appendResearchLog": {"summary": "comprei palavras"}})
        self.assertTrue(r["ok"])
        self.assertEqual([(s["key"], s["content"], s["updatedBy"]) for s in r["sections"]], [("current_goal", "crescer", "MCP")])
        self.assertEqual(r["missingSections"], ["business_overview", "positioning", "writing_preferences"])
        self.assertEqual([(c["slug"], c["content"]) for c in r["customSections"]], [("plano", "v2")])  # mesmo slug sobrescreve
        self.assertEqual([c["domain"] for c in r["competitors"]], ["zenklub-com-br.example"])
        self.assertEqual([(k["url"], k["role"]) for k in r["keyPages"]], [("https://psite13-com.example/blog", "other")])
        log = r["researchLog"][0]
        self.assertTrue(log["id"] and log["entryDate"])
        arq = mcp_grupos.pasta("projetos", "psite13") / "contexto.json"
        self.assertEqual(set(json.loads(arq.read_text(encoding="utf-8"))),
                         {"sections", "customSections", "competitors", "keyPages", "researchLog"})
        self.assertEqual(projetos.ler_contexto("psite13")["customSections"][0]["content"], "v2")
        # limpar, apagar e remover
        r = self.atualizar({"section": "current_goal", "content": ""}, {"deleteCustomSection": "plano"},
                           {"removeCompetitors": ["zenklub-com-br.example"]}, {"removeKeyPages": ["https://psite13-com.example/blog"]},
                           {"removeResearchLog": [log["id"]]})
        self.assertEqual((r["sections"], r["customSections"], r["competitors"], r["keyPages"], r["researchLog"]), ([],) * 5)

    def test_aplicar_em_python_e_modelos(self):
        projetos.aplicar("asite01", [{"section": "positioning", "content": "x"}])
        self.assertEqual(projetos.ler_contexto("asite01")["sections"][0]["key"], "positioning")
        self.assertEqual(projetos.ler_contexto("fsite05")["sections"], [])
        mcp_proxy.executar("save_report_template", {"name": "Mensal", "description": "d", "instructions": "i"})
        self.assertEqual(projetos.ler_contexto("fsite05")["reportTemplates"], [{"name": "Mensal", "description": "d"}])
        (mcp_grupos.pasta("relatorios") / "modelos" / "x.json").write_text('{"id": "x", "updatedAt": "1", "title": "t"}')
        self.assertEqual(len(projetos.ler_contexto("fsite05")["reportTemplates"]), 1)

    def test_pedido_errado(self):
        with self.assertRaises(ErroUso):
            mcp_proxy.executar("update_project_context", {"updates": [{"section": "current_goal", "content": "x"}]})
        for ops in ([], [{"section": "outra", "content": "x"}], [{"section": "current_goal", "contents": "x"}],
                    [{"section": "current_goal", "content": "x" * 4001}], [{"customSection": "Com Espaço", "content": "x"}],
                    [{"addCompetitors": [{"domain": f"c{i}.com"} for i in range(101)]}], [{"appendResearchLog": {"summary": " "}}],
                    [{"section": "current_goal", "content": "x"}] * 51, ["texto"]):
            with self.assertRaises(ErroUso, msg=ops):
                self.atualizar(*ops)
        with self.assertRaisesRegex(ErroUso, r"updates\[1\]"):  # lote atômico: nada gravado
            self.atualizar({"section": "current_goal", "content": "x"}, {"section": "x", "content": "y"})
        self.assertEqual(projetos.ler_contexto("psite13")["sections"], [])

    def test_get_project_context_traz_secoes(self):
        self.assertEqual(mcp_proxy.executar("get_project_context", {"site": "psite13"})["secoes"]["sections"], [])
        self.atualizar({"section": "business_overview", "content": "diretório"})
        c = mcp_proxy.executar("get_project_context", {"site": "psite13"})
        self.assertEqual(c["secoes"]["sections"][0]["content"], "diretório")
        self.assertEqual(c["id"], "psite13")

    def test_create_project(self):
        p = self.criar()["project"]
        self.assertEqual((p["id"], p["name"], p["domain"], p["locationCode"], p["languageCode"]),
                         ("clinica-nova", "Clínica Nova", "clinicanova-com-br.example", 2076, "pt"))
        arq = Path(self.tmp.name, "mcp", "projetos", "sites", "clinica-nova.json")
        s = json.loads(arq.read_text(encoding="utf-8"))
        self.assertEqual(sites.validar(s), [])
        self.assertFalse(s["ativo"])
        self.assertIn("clinica-nova", [x["id"] for x in sites.carregar()])
        self.assertNotIn("clinica-nova", [x["id"] for x in sites.carregar(sites.PASTA)])
        origem = {x["id"]: x["origem"] for x in mcp_proxy.executar("list_projects", {})["projects"]}
        self.assertEqual((origem["clinica-nova"], origem["psite13"]), ("mcp", "cadastro"))
        self.assertEqual(mcp_proxy.executar("get_project_context", {"site": "clinicanova-com-br.example"})["id"], "clinica-nova")
        with self.assertRaisesRegex(ErroUso, "clinica-nova"):  # repetido no MCP
            self.criar(name="Outra", domain="clinicanova-com-br.example")

    def test_create_project_repetido_ou_errado(self):
        with self.assertRaisesRegex(ErroUso, "psite13"):
            self.criar(name="psite13", domain="outro-com.example")
        with self.assertRaisesRegex(ErroUso, "psite13"):
            self.criar(name="Novo", domain="www.psite13-com.example")
        with self.assertRaisesRegex(ErroUso, "reservado"):
            self.criar(name="Sites", domain="sites-novos-com.example")
        with self.assertRaisesRegex(ErroUso, "reservado"):
            self.criar(name="Modelos", domain="modelos-novos-com.example")
        for args in ({"name": " "}, {"name": "x" * 121}, {"domain": None}, {"domain": "não é host"},
                     {"languageCode": "en"}, {"locationCode": "2076"}):
            with self.assertRaises(ErroUso, msg=args):
                self.criar(**args)
        self.assertEqual(list(Path(self.tmp.name).glob("mcp/projetos/sites/*.json")), [])

    def test_carregar_pula_arquivo_ruim_do_mcp(self):
        pst = Path(self.tmp.name, "mcp", "projetos", "sites")
        pst.mkdir(parents=True)
        (pst / "quebrado.json").write_text("{", encoding="utf-8")
        psi = json.loads((sites.PASTA / "psite13.json").read_text(encoding="utf-8"))
        (pst / "copia.json").write_text(json.dumps({**psi, "id": "copia", "ids_antigos": []}), encoding="utf-8")
        (pst / "errado.json").write_text(json.dumps({**psi, "id": "outro"}), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ids = [s["id"] for s in sites.carregar()]
        self.assertEqual(sorted(ids), sorted(s["id"] for s in sites.carregar(sites.PASTA)))
        self.assertEqual(ids.count("psite13"), 1)
        for nome in ("quebrado.json", "copia.json", "errado.json"):
            self.assertIn(nome, err.getvalue())

    def test_whoami(self):
        r = mcp_proxy.executar("whoami", {})
        self.assertEqual(set(r), {"ok", "servidor", "usuario", "sites", "ferramentas"})
        self.assertEqual((r["ok"], r["servidor"], r["usuario"]), (True, "nimbus-hub", "Ana"))
        self.assertEqual((r["sites"], r["ferramentas"]), (len(sites.carregar()), len(mcp_proxy.FERRAMENTAS)))


    # ---- research_project_website / save_project_website_setup ----

    def pesquisar(self, resposta=None, erro=None, site="fsite06", website="https://corporativo.clientea-com.example", paginas=None):
        paginas = paginas if paginas is not None else {"https://corporativo.clientea-com.example": HOME}
        baixados = []

        def baixar(url):
            baixados.append(url)
            html = paginas.get(url, "<title>Interna</title><p>texto interno</p>")
            return html and (url, html)

        with mock.patch.object(projetos, "_baixar", side_effect=baixar), \
                mock.patch.object(projetos.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
                mock.patch("hub.claude.perguntar", side_effect=erro, return_value=resposta) as perg:
            r = mcp_proxy.executar("research_project_website", {"site": site, "website": website})
        return r, baixados, perg

    def test_research_coleta_home_e_ate_5_internas_e_monta_rascunho(self):
        r, baixados, perg = self.pesquisar(resposta_ia())
        self.assertEqual(baixados[0], "https://corporativo.clientea-com.example")
        internas = baixados[1:]
        self.assertEqual(len(internas), 5)
        self.assertTrue(all(u.startswith("https://corporativo.clientea-com.example/") for u in internas))
        self.assertEqual(internas[:4], ["https://corporativo.clientea-com.example/servicos", "https://corporativo.clientea-com.example/sobre",
                                        "https://corporativo.clientea-com.example/contato", "https://corporativo.clientea-com.example/precos"])
        self.assertNotIn("https://corporativo.clientea-com.example/c.pdf", internas)
        self.assertEqual(perg.call_args.kwargs["nome"], "mcp-pesquisa-site")
        self.assertEqual(perg.call_args.kwargs["espera_trava"], 60)
        prompt = perg.call_args.args[0]
        self.assertIn("Cuidado clientem", prompt)
        self.assertIn("Fisioterapia e pilates", prompt)
        self.assertNotIn('var x = \\"nao\\"', prompt)
        self.assertTrue(r["ok"])
        self.assertEqual((r["name"], r["domain"], r["preserveCompetitors"]), ("Cliente A Corporativo", "corporativo.clientea-com.example", False))
        self.assertEqual(len(r["overview"]), 4000)
        self.assertEqual(r["competitors"], [{"name": "Concorrente", "domain": "concorrente-com-br.example", "notes": "também faz fsite05",
                                             "sourceUrl": "https://concorrente-com-br.example/fsite05"}])
        nomes = [t["name"] for t in r["suggestedTopics"]]
        self.assertEqual(nomes, ["fisioterapia em casa", "tema 0", "tema 1", "tema 2", "tema 3"])
        self.assertTrue(all(len(t["prompts"]) == 5 for t in r["suggestedTopics"]))
        self.assertEqual(len(r["suggestedKeywords"]), 15)
        self.assertNotIn("frase com seis palavras demais aqui", r["suggestedKeywords"])
        self.assertEqual(list(Path(self.tmp.name).rglob("*")), [])  # nada gravado

    def test_research_site_com_concorrentes_preserva(self):
        r, _, perg = self.pesquisar(resposta_ia(), site="psite13", website="psite13-com.example")
        self.assertEqual((r["preserveCompetitors"], r["competitors"]), (True, []))
        self.atualizar({"section": "business_overview", "content": "já escrito"},
                       {"customSection": "pesquisa-do-site", "title": "Pesquisa do site", "content": "x"})
        r, baixados, perg = self.pesquisar(resposta_ia(), site="psite13", website="psite13-com.example")
        perg.assert_not_called()  # nada falta: sem IA nem coleta
        self.assertEqual((r["ok"], r["overview"], r["suggestedTopics"], baixados), (True, "já escrito", [], []))

    def test_research_erros_da_ia_e_do_site(self):
        self.assertEqual(self.pesquisar("não é json")[0]["reason"], "ia_invalida")
        self.assertEqual(self.pesquisar(json.dumps({"overview": ""}))[0]["reason"], "ia_invalida")
        from hub import claude
        for erro, motivo in ((claude.ContaPausada("p"), "conta_pausada"), (claude.ContaOcupada("o"), "conta_ocupada"),
                             (RuntimeError("claude saiu com 1"), "ia_erro")):
            r = self.pesquisar(erro=erro)[0]
            self.assertEqual((r["ok"], r["reason"]), (False, motivo))
        r = self.pesquisar(resposta_ia(), paginas={"https://corporativo.clientea-com.example": None})[0]
        self.assertEqual((r["ok"], r["reason"]), (False, "site_ilegivel"))
        self.assertTrue(r["message"])

    def test_research_pedido_errado(self):
        for website in ("ftp://corporativo.clientea-com.example", "file:///etc/passwd", "", "x" * 2001):
            with self.assertRaises(ErroUso, msg=website):
                self.pesquisar(resposta_ia(), website=website)
        with self.assertRaises(ErroUso):
            mcp_proxy.executar("research_project_website", {"website": "https://corporativo.clientea-com.example"})
        with mock.patch.object(projetos, "_baixar") as baixar:
            for ip in ("http://127.0.0.1/", "http://10.0.0.5", "http://169.254.169.254/latest", "http://[::1]/"):
                with self.assertRaises(ErroUso, msg=ip):
                    mcp_proxy.executar("research_project_website", {"site": "fsite06", "website": ip})
            baixar.assert_not_called()

    def test_baixar_recusa_ip_interno_na_conexao(self):
        import http.server
        import threading
        srv = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertIsNone(projetos._baixar(f"http://127.0.0.1:{srv.server_port}/"))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_baixar_recusa_redirecionamento_para_ftp(self):
        import http.server
        import threading

        class Redir(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "ftp://127.0.0.1/segredo")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Redir)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(projetos, "_ip_publico"), \
                    mock.patch("urllib.request.FTPHandler.ftp_open", side_effect=AssertionError("ftp aberto")) as ftp:
                self.assertIsNone(projetos._baixar(f"http://127.0.0.1:{srv.server_port}/"))
                ftp.assert_not_called()
                self.assertEqual(projetos._coletar(f"http://127.0.0.1:{srv.server_port}/"), None)  # home: site_ilegivel
        finally:
            srv.shutdown()
            srv.server_close()
        for esquema in ("ftp", "file", "data"):
            self.assertFalse(any(hasattr(h, f"{esquema}_open") for h in projetos._ABRIR.handlers), esquema)

    def salvar(self, site="fsite06", **args):
        base = {"name": "Instituto", "domain": "corporativo.clientea-com.example", "overview": "Visão nova",
                "competitors": [{"name": "Conc", "domain": "https://www.conc-com-br.example/"}, {"name": "Conc 2", "domain": "conc-com-br.example"}],
                "suggestedTopics": [topico("fisioterapia")], "suggestedKeywords": ["pilates", "dor nas costas"]}
        return mcp_proxy.executar("save_project_website_setup", {"site": site, **base, **args})

    def test_save_preenche_o_que_falta(self):
        r = self.salvar()
        self.assertTrue(r["ok"])
        p = r["project"]
        self.assertEqual((p["id"], p["domain"]), ("fsite06", "corporativo.clientea-com.example"))
        self.assertEqual([(s["key"], s["content"]) for s in p["sections"]], [("business_overview", "Visão nova")])
        self.assertEqual([(c["domain"], c["name"], c["notes"]) for c in p["competitors"]], [("conc-com-br.example", "Conc 2", "")])
        [sec] = p["customSections"]
        self.assertEqual((sec["slug"], sec["title"]), ("pesquisa-do-site", "Pesquisa do site"))
        self.assertIn("fisioterapia pergunta 4", sec["content"])
        self.assertIn("dor nas costas", sec["content"])
        # existentes são autoritativos
        r = self.salvar(overview="Outra", competitors=[{"name": "X", "domain": "x.com"}], suggestedKeywords=["outra"])
        p = r["project"]
        self.assertEqual(p["sections"][0]["content"], "Visão nova")
        self.assertEqual([c["domain"] for c in p["competitors"]], ["conc-com-br.example"])
        self.assertNotIn("outra", p["customSections"][0]["content"])
        self.assertEqual(list(Path(self.tmp.name).glob("mcp/projetos/sites/*.json")), [])

    def test_save_pedido_errado(self):
        for args in ({"overview": ""}, {"name": ""}, {"competitors": [{"name": "Eu", "domain": "corporativo.clientea-com.example"}]},
                     {"suggestedTopics": [topico("tema", 4)]}, {"suggestedKeywords": ["seis palavras é demais para isso"]},
                     {"competitors": [{"name": "C", "domain": "c-com.example", "notes": "n" * 501}]}):
            with self.assertRaises(ErroUso, msg=args):
                self.salvar(**args)
        self.assertEqual(projetos.ler_contexto("fsite06")["sections"], [])


if __name__ == "__main__":
    unittest.main()
