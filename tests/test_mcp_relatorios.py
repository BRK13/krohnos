import json
import os
import tempfile
import unittest
from unittest import mock

from hub import mcp_grupos, mcp_proxy
from hub.mcp_grupos import ErroUso

HTML = "<html><body>oi ção</body></html>"
PROPS = {  # propriedades do original (mais site)
    "save_report": {"title", "summary", "html", "reportId", "skill", "templateId"},
    "list_reports": {"limit", "offset"},
    "get_report": {"reportId", "includeHtml"},
    "delete_report": {"reportId"},
    "set_report_sharing": {"reportId", "public"},
    "save_report_template": {"templateId", "name", "description", "instructions"},
    "list_report_templates": set(),
    "delete_report_template": {"templateId"},
}


class Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HUB_DATA": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def chamar(self, nome, **args):
        return mcp_proxy.executar(nome, {"site": "psite15", **args})

    def salvar(self, titulo="Auditoria", **kw):
        return self.chamar("save_report", title=titulo, summary="resumo", html=HTML, **kw)

    def test_schemas_iguais_ao_original(self):
        for nome, props in PROPS.items():
            esquema = mcp_proxy.FERRAMENTAS[nome]["inputSchema"]
            self.assertEqual(set(esquema["properties"]), props | {"site", "projectId"}, nome)
        sr = mcp_proxy.FERRAMENTAS["save_report"]["inputSchema"]
        self.assertEqual(sr["required"], ["title", "summary", "html"])
        self.assertEqual(sr["properties"]["skill"]["maxLength"], 60)
        lr = mcp_proxy.FERRAMENTAS["list_reports"]["inputSchema"]["properties"]["limit"]
        self.assertEqual((lr["minimum"], lr["maximum"]), (1, 50))

    def test_ciclo_do_relatorio(self):
        s = self.salvar(skill="seo-audit")
        self.assertTrue(s["created"])
        self.assertEqual(s["htmlBytes"], len(HTML.encode()))
        self.assertEqual(s["url"], "https://painel.exemplo.com.br/")
        arq = mcp_grupos.pasta("relatorios", "psite15") / f"{s['reportId']}.json"
        self.assertIn("ção", arq.read_text(encoding="utf-8"))  # ensure_ascii=False
        self.assertIsNone(json.loads(arq.read_text(encoding="utf-8"))["shareToken"])
        lista = self.chamar("list_reports")
        self.assertEqual((lista["totalCount"], lista["rowCount"]), (1, 1))
        self.assertNotIn("html", lista["reports"][0])
        r = self.chamar("get_report", reportId=s["reportId"])["report"]
        self.assertEqual((r["title"], r["skill"], r["shareUrl"], "html" in r), ("Auditoria", "seo-audit", None, False))
        self.assertEqual(self.chamar("get_report", reportId=s["reportId"], includeHtml=True)["report"]["html"], HTML)
        self.assertEqual(self.chamar("delete_report", reportId=s["reportId"]), {"reportId": s["reportId"], "deleted": True})
        self.assertEqual(self.chamar("list_reports")["totalCount"], 0)

    def test_substituir_mantem_skill_e_titulo_repetido_e_recusado(self):
        s = self.salvar(skill="seo-audit")
        s2 = self.salvar(titulo="Novo", reportId=s["reportId"])
        self.assertEqual((s2["reportId"], s2["created"]), (s["reportId"], False))
        self.assertEqual(self.chamar("get_report", reportId=s["reportId"])["report"]["skill"], "seo-audit")
        self.salvar("Outro")
        with self.assertRaises(ErroUso):
            self.salvar("Outro")

    def test_relatorios_sao_por_site(self):
        s = self.salvar()
        with self.assertRaises(ErroUso):
            mcp_proxy.executar("get_report", {"site": "asite01", "reportId": s["reportId"]})

    def test_pedido_errado_do_relatorio(self):
        s = self.salvar()
        erros = [("save_report", {"title": "a", "summary": "b", "html": HTML, "site": None}),
                 ("get_report", {"reportId": "nao-existe"}), ("delete_report", {"reportId": "nao-existe"}),
                 ("get_report", {"reportId": "../x"}), ("delete_report", {"reportId": "../../etc/passwd"}),
                 ("save_report", {"title": "a", "summary": "b", "html": HTML, "reportId": "../x"}),
                 ("save_report", {"title": "a", "summary": "b"}), ("save_report", {"summary": "b", "html": HTML}),
                 ("save_report", {"title": "a", "summary": "b", "html": "<html>sem fim"}),
                 ("save_report", {"title": "x" * 121, "summary": "b", "html": HTML}),
                 ("save_report", {"title": "a", "summary": "b", "html": HTML, "templateId": "nao-existe"}),
                 ("list_reports", {"limit": "muito"})]
        for nome, args in erros:
            with self.subTest(nome=nome, args=args), self.assertRaises(ErroUso):
                mcp_proxy.executar(nome, {"site": "psite15", **args} if args.get("site", 1) else {k: v for k, v in args.items() if k != "site"})
        self.assertEqual(self.chamar("get_report", reportId=s["reportId"])["report"]["id"], s["reportId"])

    def test_paginacao(self):
        for i in range(3):
            self.salvar(f"R{i}")
        p = self.chamar("list_reports", limit=2, offset=1)
        self.assertEqual((p["totalCount"], p["rowCount"]), (3, 2))

    def compartilhar(self, rid, publico):
        return self.chamar("set_report_sharing", reportId=rid, public=publico)

    def test_save_report_compartilhado_devolve_o_link(self):
        rid = self.salvar()["reportId"]
        link = self.compartilhar(rid, True)["shareUrl"]
        self.assertEqual(self.salvar(reportId=rid, titulo="Outro")["url"], link)

    def test_site_modelos_recusado_e_json_solto_nao_e_modelo(self):
        with self.assertRaisesRegex(ErroUso, "reservado"):
            mcp_grupos.relatorios.executar("list_reports", {}, {"id": "modelos"})
        mcp_proxy.executar("save_report_template", {"name": "N", "description": "d", "instructions": "i"})
        (mcp_grupos.pasta("relatorios") / "modelos" / "x.json").write_text('{"id": "x", "updatedAt": "1", "title": "t"}')
        self.assertEqual([t["name"] for t in mcp_proxy.executar("list_report_templates", {})["templates"]], ["N"])

    def test_compartilhar_liga_mantem_desliga_e_religa(self):
        rid = self.salvar()["reportId"]
        arq = mcp_grupos.pasta("relatorios", "psite15") / f"{rid}.json"
        on = self.compartilhar(rid, True)
        tok = json.loads(arq.read_text(encoding="utf-8"))["shareToken"]
        self.assertGreaterEqual(len(tok), 30)
        self.assertEqual(on, {"reportId": rid, "public": True, "url": f"https://painel.exemplo.com.br/r/{tok}",
                              "shareUrl": f"https://painel.exemplo.com.br/r/{tok}"})
        self.assertTrue(json.loads(arq.read_text(encoding="utf-8"))["sharedAt"])
        self.assertEqual(self.compartilhar(rid, True)["shareUrl"], on["shareUrl"])
        self.assertEqual(self.chamar("get_report", reportId=rid)["report"]["shareUrl"], on["shareUrl"])
        self.assertNotIn("shareToken", self.chamar("list_reports")["reports"][0])
        off = self.compartilhar(rid, False)
        self.assertEqual(off, {"reportId": rid, "public": False, "url": "https://painel.exemplo.com.br/", "shareUrl": None})
        d = json.loads(arq.read_text(encoding="utf-8"))
        self.assertEqual((d["shareToken"], d["sharedAt"]), (None, None))
        self.assertIsNone(self.chamar("get_report", reportId=rid)["report"]["shareUrl"])
        self.assertNotEqual(self.compartilhar(rid, True)["shareUrl"], on["shareUrl"])

    def test_substituir_mantem_o_link(self):
        s = self.salvar()
        url = self.compartilhar(s["reportId"], True)["shareUrl"]
        self.salvar("Novo", reportId=s["reportId"])
        self.assertEqual(self.chamar("get_report", reportId=s["reportId"])["report"]["shareUrl"], url)

    def test_pedido_errado_do_compartilhamento(self):
        rid = self.salvar()["reportId"]
        for args in ({"reportId": "nao-existe", "public": True}, {"reportId": rid, "public": "true"},
                     {"reportId": rid, "public": 1}, {"reportId": rid}, {"reportId": "../x", "public": True}):
            with self.subTest(args=args), self.assertRaises(ErroUso):
                self.chamar("set_report_sharing", **args)
        with self.assertRaises(ErroUso):
            mcp_proxy.executar("set_report_sharing", {"reportId": rid, "public": True})

    def test_ciclo_do_modelo_e_globalidade(self):
        m = mcp_proxy.executar("save_report_template", {"name": "Resumo", "description": "d", "instructions": "i"})
        self.assertEqual((m["created"], m["url"]), (True, "https://painel.exemplo.com.br/"))
        arq = mcp_grupos.pasta("relatorios") / "modelos" / f"{m['templateId']}.json"
        self.assertTrue(arq.exists())
        lista = mcp_proxy.executar("list_report_templates", {"site": "asite01"})["templates"]
        self.assertEqual([t["name"] for t in lista], ["Resumo"])
        self.assertIn("instructionsPreview", lista[0])
        self.assertTrue(self.salvar(templateId=m["templateId"])["created"])
        up = mcp_proxy.executar("save_report_template", {"templateId": m["templateId"], "name": "Resumo 2", "description": "d", "instructions": "i"})
        self.assertFalse(up["created"])
        r = mcp_proxy.executar("delete_report_template", {"templateId": m["templateId"]})
        self.assertEqual(r, {"templateId": m["templateId"], "deleted": True})
        self.assertEqual(mcp_proxy.executar("list_report_templates", {})["templates"], [])

    def test_pedido_errado_do_modelo(self):
        ok = {"name": "N", "description": "d", "instructions": "i"}
        mcp_proxy.executar("save_report_template", ok)
        casos = [("save_report_template", {"description": "d", "instructions": "i"}),
                 ("save_report_template", {**ok, "name": "n"}),  # nome repetido
                 ("save_report_template", {**ok, "name": "x" * 81}),
                 ("save_report_template", {**ok, "description": "x" * 201}),
                 ("save_report_template", {**ok, "instructions": "x" * 3001}),
                 ("save_report_template", {**ok, "name": "Z", "templateId": "nao-existe"}),
                 ("save_report_template", {**ok, "name": "Z", "templateId": "../x"}),
                 ("delete_report_template", {"templateId": "nao-existe"}),
                 ("delete_report_template", {"templateId": "../x"}), ("delete_report_template", {})]
        for nome, args in casos:
            with self.subTest(nome=nome, args=args), self.assertRaises(ErroUso):
                mcp_proxy.executar(nome, args)

    def test_limite_de_modelos(self):
        for i in range(10):
            mcp_proxy.executar("save_report_template", {"name": f"N{i}", "description": "d", "instructions": "i"})
        with self.assertRaises(ErroUso):
            mcp_proxy.executar("save_report_template", {"name": "N10", "description": "d", "instructions": "i"})


if __name__ == "__main__":
    unittest.main()
