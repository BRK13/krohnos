import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import agenda, linear, skills, tarefas as T


def skill(lib, nome, desc="Faz x. Depois y.", extra=None):
    d = Path(lib) / nome
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {nome}\ndescription: {desc}\n---\ncorpo {nome}\n", encoding="utf-8")
    for k, v in (extra or {}).items():
        (d / k).write_text(v, encoding="utf-8")
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)
        self.lib = self.raiz / "lib"
        for n in ("x", "y"):
            skill(self.lib, n)
        p = mock.patch.dict(os.environ, {"HUB_SKILLS_BIBLIOTECA": str(self.lib)})
        p.start()
        self.addCleanup(p.stop)


class MontarTest(Base):
    def nomes(self, r):
        return sorted(p.name for p in (Path(r["plugin"]) / "skills").iterdir())

    def test_sem_skills_so_o_base(self):
        r = skills.montar(self.raiz / "a", [])
        self.assertEqual(self.nomes(r), [])
        self.assertEqual(json.loads((Path(r["plugin"]) / ".claude-plugin" / "plugin.json").read_text())["name"], "tarefa")
        with mock.patch.object(skills, "base", lambda: ["y"]):
            self.assertEqual(self.nomes(skills.montar(self.raiz / "b", [])), ["y"])

    def test_ve_x_e_nao_y_e_desconhecida_vai_para_faltando(self):
        r = skills.montar(self.raiz / "a", ["x", "x", "nao-existe", "../x", "a/b"])
        self.assertEqual(self.nomes(r), ["x"])
        self.assertEqual(r["ligadas"], ["x"])
        self.assertEqual(r["faltando"], ["nao-existe", "../x", "a/b"])
        self.assertTrue((Path(r["plugin"]) / "skills" / "x" / "SKILL.md").is_file())

    def test_duas_montagens_simultaneas_nao_se_misturam(self):
        a, b = skills.montar(self.raiz / "a", ["x"]), skills.montar(self.raiz / "b", ["y"])
        self.assertEqual((self.nomes(a), self.nomes(b)), (["x"], ["y"]))


class SettingsTest(Base):
    def test_desliga_plugins_fora_do_base_e_poe_ttl(self):
        conta = self.raiz / "conta"
        conta.mkdir()
        (conta / "settings.json").write_text(json.dumps({"enabledPlugins": {
            "superpowers@claude-plugins-official": True, "code-review@claude-plugins-official": True,
            "frontend-design@claude-plugins-official": True, "ponytail@ponytail": True}}))
        j = json.loads(skills.settings_json(conta))
        self.assertEqual(j["subagentPromptCacheTtl"], "5m")
        self.assertEqual(j["enabledPlugins"], {"frontend-design@claude-plugins-official": False, "ponytail@ponytail": False})

    def test_sem_settings_da_conta(self):
        self.assertEqual(json.loads(skills.settings_json(self.raiz / "nada"))["enabledPlugins"], {})


    def test_exclui_claude_md_de_projeto_duplicado(self):
        (self.raiz / ".claude").mkdir()
        (self.raiz / ".claude-2").mkdir()
        for c in (".claude", ".claude-2"):
            (self.raiz / c / "CLAUDE.md").write_text("@RTK.md")
            (self.raiz / c / "RTK.md").write_text("x")
        j = json.loads(skills.settings_json(self.raiz / ".claude-2"))
        self.assertEqual(j["claudeMdExcludes"], [str(self.raiz / ".claude" / "CLAUDE.md"), str(self.raiz / ".claude" / "RTK.md")])
        self.assertNotIn("claudeMdExcludes", json.loads(skills.settings_json(self.raiz / ".claude")))

    def test_conta_sem_claude_md_proprio_mantem_o_de_projeto(self):
        (self.raiz / ".claude").mkdir()
        (self.raiz / ".claude" / "CLAUDE.md").write_text("x")
        (self.raiz / ".claude-3").mkdir()
        self.assertNotIn("claudeMdExcludes", json.loads(skills.settings_json(self.raiz / ".claude-3")))

    def test_claude_md_diferente_nao_e_excluido(self):
        for c in (".claude", ".claude-2"):
            (self.raiz / c).mkdir()
            (self.raiz / c / "RTK.md").write_text("x")
        (self.raiz / ".claude" / "CLAUDE.md").write_text("instrução só da primeira")
        (self.raiz / ".claude-2" / "CLAUDE.md").write_text("outra")
        j = json.loads(skills.settings_json(self.raiz / ".claude-2"))
        self.assertEqual(j["claudeMdExcludes"], [str(self.raiz / ".claude" / "RTK.md")])


class ListarTest(Base):
    def test_descricao_de_varias_linhas(self):
        (self.lib / "z").mkdir()
        (self.lib / "z" / "SKILL.md").write_text("---\nname: z\ndescription: >\n  Primeira linha do bloco.\n  Segunda.\nlicense: x\n---\n")
        (self.lib / "w").mkdir()
        (self.lib / "w" / "SKILL.md").write_text("---\nname: w\ndescription: |\n  Linha um\n  linha dois\n---\n")
        (self.lib / "sem").mkdir()  # sem SKILL.md: fora
        d = dict(skills.listar())
        self.assertEqual(d["z"], "Primeira linha do bloco.")
        self.assertEqual(d["w"], "Linha um")
        self.assertEqual(d["x"], "Faz x.")
        self.assertNotIn("sem", d)
        self.assertEqual([n for n, _ in skills.listar()], sorted(d))


class MigrarTest(Base):
    def setUp(self):
        super().setUp()
        self.home = self.raiz / "home"
        self.cont = {n: self.home / (".claude" if n == "1" else f".claude-{n}") for n in "1234"}
        for c in self.cont.values():
            (c / "skills").mkdir(parents=True)
        self.alvo = self.raiz / "alvo"
        skill(self.alvo, "viaja", extra={"ref.md": "r"})
        p = mock.patch("hub.conta.faixa", lambda n: "operador" if str(n) == "4" else "construtor")
        p.start()
        self.addCleanup(p.stop)

    def roda(self, aplicar):
        return skills.migrar(aplicar=aplicar, home=self.home)

    def monta(self):
        skill(self.cont["1"] / "skills", "novo")
        skill(self.cont["1"] / "skills", "x")                     # igual à da biblioteca: dup
        skill(self.cont["2"] / "skills", "x")                     # dup também
        skill(self.cont["2"] / "skills", "y", desc="Diferente.")  # conflito
        skill(self.cont["3"] / "skills", "novo")                  # igual à que a conta 1 leva: dup
        (self.cont["3"] / "skills" / "link").symlink_to(self.alvo / "viaja", target_is_directory=True)
        (self.cont["3"] / "skills" / "quebrado").symlink_to(self.raiz / "nao-existe")
        skill(self.cont["4"] / "skills", "operador")              # faixa operador: intocada

    def test_dry_run_nao_mexe(self):
        self.monta()
        plano = self.roda(False)
        acoes = {(r["conta"], r["nome"]): r["acao"] for r in plano}
        self.assertEqual(acoes[("1", "novo")], "mover")
        self.assertEqual(acoes[("1", "x")], "remover-dup")
        self.assertEqual(acoes[("2", "y")], "conflito")
        self.assertEqual(acoes[("3", "novo")], "remover-dup")
        self.assertEqual(acoes[("3", "link")], "mover")
        self.assertEqual(acoes[("3", "quebrado")], "quebrado")
        self.assertFalse(any(c == "4" for c, _ in acoes))
        self.assertFalse((self.lib / "novo").exists())
        self.assertTrue((self.cont["1"] / "skills" / "x").exists())
        self.assertTrue((self.cont["3"] / "skills" / "link").is_symlink())

    def test_aplicar(self):
        self.monta()
        self.roda(True)
        self.assertTrue((self.lib / "novo" / "SKILL.md").is_file())
        self.assertFalse((self.cont["1"] / "skills" / "novo").exists())
        self.assertFalse((self.cont["1"] / "skills" / "x").exists())
        self.assertFalse((self.cont["2"] / "skills" / "x").exists())
        self.assertFalse((self.cont["3"] / "skills" / "novo").exists())
        self.assertTrue((self.cont["2"] / "skills" / "y").exists())  # conflito fica
        self.assertIn("corpo y", (self.lib / "y" / "SKILL.md").read_text())  # a da biblioteca vale
        self.assertFalse((self.lib / "link").is_symlink())
        self.assertEqual((self.lib / "link" / "ref.md").read_text(), "r")  # conteúdo, não link
        self.assertFalse(os.path.lexists(self.cont["3"] / "skills" / "link"))
        self.assertFalse(os.path.lexists(self.cont["3"] / "skills" / "quebrado"))
        self.assertTrue((self.alvo / "viaja" / "SKILL.md").exists())  # o alvo original não some
        self.assertTrue((self.cont["4"] / "skills" / "operador").exists())
        self.assertEqual([r["acao"] for r in self.roda(True) if r["acao"] == "mover"], [])

    def test_copia_skills_de_plugin_fora_do_base(self):
        for nome, base_ in (("frontend-design", False), ("superpowers", True)):
            inst = self.raiz / "plug" / nome
            skill(inst / "skills", nome)
            (self.cont["1"] / "plugins").mkdir(exist_ok=True)
        (self.cont["1"] / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {
            "frontend-design@claude-plugins-official": [{"installPath": str(self.raiz / "plug" / "frontend-design")}],
            "superpowers@claude-plugins-official": [{"installPath": str(self.raiz / "plug" / "superpowers")}]}}))
        plano = self.roda(False)
        self.assertEqual([r["nome"] for r in plano if r["acao"] == "copiar-plugin"], ["frontend-design"])
        self.assertFalse((self.lib / "frontend-design").exists())
        self.roda(True)
        self.assertTrue((self.lib / "frontend-design" / "SKILL.md").is_file())
        self.assertFalse((self.lib / "superpowers").exists())


class ChamarClaudeTest(Base):
    def roda(self, skills_arg):
        vistos = {}

        def falso(cmd, cwd, env, timeout_s, ao_vivo=None, traduzir=None, entrada=None):
            vistos["cmd"] = cmd
            i = cmd.index("--plugin-dir") if "--plugin-dir" in cmd else None
            if i is not None:
                vistos["plugin_existe"] = Path(cmd[i + 1], ".claude-plugin", "plugin.json").is_file()
                vistos["ligadas"] = sorted(p.name for p in Path(cmd[i + 1], "skills").iterdir())
            return None, 0, "", ""
        with mock.patch.object(agenda, "_rodar_cli", falso), mock.patch.object(agenda, "config_dir", lambda: self.raiz / "conta"), \
                mock.patch.object(agenda, "_desfecho", lambda *a, **k: (0, {}, "")):
            agenda.chamar_claude("p", "sonnet", 60, "/tmp", {}, skills=skills_arg)
        return vistos

    def test_cmd_tem_plugin_dir_e_settings_e_a_pasta_some(self):
        v = self.roda(["x"])
        cmd = v["cmd"]
        self.assertIn("--settings", cmd)
        self.assertEqual(json.loads(cmd[cmd.index("--settings") + 1])["subagentPromptCacheTtl"], "5m")
        self.assertTrue(v["plugin_existe"])
        self.assertEqual(v["ligadas"], ["x"])
        pasta = Path(cmd[cmd.index("--plugin-dir") + 1]).parent
        self.assertTrue(pasta.name.startswith("skills-"))
        self.assertFalse(pasta.exists())

    def test_lista_vazia_tambem_monta_e_none_nao(self):
        self.assertEqual(self.roda([])["ligadas"], [])
        self.assertNotIn("--plugin-dir", self.roda(None)["cmd"])

    def test_pasta_some_mesmo_com_erro(self):
        def quebra(*a, **k):
            raise RuntimeError("x")
        criadas = []
        orig = tempfile.mkdtemp
        with mock.patch.object(agenda, "_rodar_cli", quebra), mock.patch.object(agenda.tempfile, "mkdtemp",
                                                                              lambda **k: criadas.append(orig(**k)) or criadas[-1]):
            with self.assertRaises(RuntimeError):
                agenda.chamar_claude("p", "sonnet", 60, "/tmp", {}, skills=[])
        self.assertFalse(Path(criadas[0]).exists())


class PromptSkillsTest(Base):
    def test_secao_lista_base_e_da_tarefa(self):
        t = T.parse("---\nquando: manual\nprioridade: 2\nmodelo: sonnet\ntimeout: 1h\nexterno: false\nskills: x, y\n"
                    "descricao: d\n---\nCorpo.\n", "teste")
        with tempfile.TemporaryDirectory() as d:
            p = agenda.montar_prompt(t, d)
        self.assertIn("## Skills desta execução", p)
        self.assertIn("superpowers, code-review, x, y", p)
        self.assertIn("python3 -m hub.skills listar", p)
        self.assertIn("anote no texto final", p)  # tarefa agendada: sem issue
        t.linear = {"x": 1}
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("comente na issue `precisa skill:<nome>`", agenda.montar_prompt(t, d))


class PromptSkillsForaTest(Base):
    def test_pedida_que_nao_existe_vai_em_linha_separada(self):
        t = T.parse("---\nquando: manual\nprioridade: 2\nmodelo: sonnet\ntimeout: 1h\nexterno: false\nskills: x, typo\n"
                    "descricao: d\n---\nCorpo.\n", "teste")
        with tempfile.TemporaryDirectory() as d:
            p = agenda.montar_prompt(t, d)
        self.assertIn("superpowers, code-review, x\n", p)
        self.assertIn("pedidas mas fora da biblioteca: typo", p)
        self.assertNotIn("x, typo", p)


class TarefaESkillTest(Base):
    def test_header_skills(self):
        t = T.parse("---\nquando: manual\nprioridade: 2\nmodelo: sonnet\ntimeout: 1h\nexterno: false\n"
                    "skills: frontend-design, ns:x # c\ndescricao: d\n---\nC.\n", "teste")
        self.assertEqual(t.skills, ["frontend-design", "ns:x"])
        self.assertEqual(T.parse(t_sem := "---\nquando: manual\nprioridade: 2\nmodelo: sonnet\ntimeout: 1h\nexterno: false\n"
                                 "descricao: d\n---\nC.\n", "teste").skills, [])
        with self.assertRaises(T.TarefaInvalida):
            T.parse(t_sem.replace("descricao", "skills: Ruim/x\ndescricao"), "teste")

    def test_labels_viram_skills(self):
        issue = {"id": "u", "identifier": "KRN-1", "title": "t", "url": "x", "comments": {"nodes": []},
                 "team": {"states": {"nodes": []}},
                 "labels": {"nodes": [{"name": "skill:frontend-design"}, {"name": "Skill:ab"}, {"name": "merge"}]}}
        item = linear.item_da_issue(issue)
        self.assertEqual(item["skills"], ["ab", "frontend-design"])
        item.update({"comentarios": [], "repos": []})
        self.assertEqual(linear.tarefa(item).skills, ["ab", "frontend-design"])

    def test_ligar_skills_cria_label_e_nao_repete(self):
        chamadas = []

        def gql(q, app=False, **v):
            chamadas.append((q.split("(")[0].split("{")[0].strip() or q[:20], v))
            if "teams(" in q:
                return {"teams": {"nodes": [{"id": "T", "labels": {"nodes": [{"id": "L1", "name": "skill:a"}]}}]}}
            if "issueLabels(" in q:
                return {"issueLabels": {"nodes": []}}
            if "issueLabelCreate" in q:
                return {"issueLabelCreate": {"issueLabel": {"id": "L2"}}}
            if "issueAddLabel" in q:
                return {}
            return {"issue": {"id": "I", "labels": {"nodes": [{"name": "skill:a"}]}}}
        with mock.patch.object(linear, "gql", gql):
            self.assertEqual(linear.ligar_skills("KRN-1", ["a", "b"]), ["b"])
            with self.assertRaises(SystemExit):
                linear.ligar_skills("KRN-1", ["../x"])
        add = [v for q, v in chamadas if "issueAddLabel" in q or v.get("l")]
        self.assertEqual(add, [{"i": "I", "l": "L2"}])


if __name__ == "__main__":
    unittest.main()
