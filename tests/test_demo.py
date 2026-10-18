"""kronos demo: popula, marca, limpa, recusa dado real, faixa em toda página, série de 30 dias determinística. Sem rede."""
import datetime as dt
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from hub import armazem, demo, metas, painel, propostas, sites, ui
from tests.test_painel import cockpit_neutro

FIM = "2026-10-15"
PORTA = ["--fim", FIM]


def arvore(raiz):
    """{caminho relativo: conteúdo} (arquivo) ou None (pasta): o diretório inteiro, para comparar antes e depois."""
    return {str(p.relative_to(raiz)): (p.read_bytes() if p.is_file() else None) for p in sorted(Path(raiz).rglob("*"))}


class DemoTest(unittest.TestCase):
    def setUp(self):
        cockpit_neutro(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name) / "dados"
        self.d.mkdir()
        os.environ.pop("KRONOS_PROJETOS", None)
        for p in (mock.patch.dict(os.environ, {"HUB_DATA": str(self.d), "KRONOS_CAMINHOS_DADOS": str(self.d)}),
                  mock.patch.object(sites, "PASTA", self.d / "projetos")):
            p.start()
            self.addCleanup(p.stop)

    def rodar(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = demo.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def fatos(self):
        with closing(armazem.abrir(self.d)) as c:
            return c.execute("select dia, negocio, fonte, metrica, dim, valor from fato order by 1,2,3,4").fetchall()

    def test_popula_tudo_marcado_demo(self):
        rc, _, _ = self.rodar(*PORTA)
        self.assertEqual(rc, 0)
        marca = json.loads((self.d / "demo.json").read_text(encoding="utf-8"))
        self.assertIs(marca["demo"], True)
        self.assertEqual(sorted(marca["projetos"]), ["exemplo-clinica", "exemplo-loja"])
        self.assertEqual(len(sites.carregar(self.d / "projetos")), 2)
        self.assertEqual(len(metas.listar(self.d)), 3)
        self.assertTrue(all(m["demo"] is True for m in metas.listar(self.d)))
        ps = propostas.listar(base_dados=self.d)
        self.assertEqual(len(ps), 2)
        self.assertTrue(all(p["demo"] is True for p in ps))
        self.assertTrue((self.d / "demo" / "recibos" / "DEMO-1.json").is_file())
        # nada com e-mail ou domínio de verdade
        for arq in self.d.rglob("*.json"):
            for email in re.findall(r"[\w.+-]+@[\w.-]+", arq.read_text(encoding="utf-8")):
                self.assertTrue(email.endswith(".test"), f"{arq.name}: {email}")

    def test_serie_30_dias_exata_e_deterministica(self):
        self.rodar(*PORTA)
        a = self.fatos()
        for negocio in ("exemplo-loja", "exemplo-clinica"):
            dias = {d for d, n, f, m, _, _ in a if n == negocio and m == "cliques"}
            self.assertEqual(len(dias), 30)
            self.assertEqual(max(dias), FIM)
            self.assertEqual(min(dias), (dt.date.fromisoformat(FIM) - dt.timedelta(days=29)).isoformat())
        self.assertEqual(len(a), 2 * 30 * 6)
        self.assertEqual(demo.serie(852, dt.date.fromisoformat(FIM)), demo.serie(852, dt.date.fromisoformat(FIM)))
        self.assertNotEqual(demo.serie(852, dt.date.fromisoformat(FIM)), demo.serie(7, dt.date.fromisoformat(FIM)))
        # mesma semente em outro diretório: mesmos números
        outro = Path(self.tmp.name) / "outro"
        with mock.patch.dict(os.environ, {"KRONOS_CAMINHOS_DADOS": str(outro)}):
            self.assertEqual(self.rodar("--dados", str(outro), "--semente", "852", *PORTA)[0], 0)
        with closing(armazem.abrir(outro)) as c:
            self.assertEqual(c.execute("select dia, negocio, fonte, metrica, dim, valor from fato order by 1,2,3,4").fetchall(), a)
        # semente diferente: números diferentes
        outro2 = Path(self.tmp.name) / "outro2"
        with mock.patch.dict(os.environ, {"KRONOS_CAMINHOS_DADOS": str(outro2)}):
            self.rodar("--dados", str(outro2), "--semente", "7", *PORTA)
        with closing(armazem.abrir(outro2)) as c:
            self.assertNotEqual(c.execute("select dia, negocio, fonte, metrica, dim, valor from fato order by 1,2,3,4").fetchall(), a)

    def test_segunda_execucao_idempotente(self):
        self.rodar(*PORTA)
        sem_banco = lambda: {k: v for k, v in arvore(self.d).items() if k != "bi/metricas.db"}  # bytes do SQLite mudam à toa
        antes, fatos = sem_banco(), self.fatos()
        rc, _, _ = self.rodar(*PORTA)
        self.assertEqual(rc, 0)
        self.assertEqual(sem_banco(), antes)
        self.assertEqual(self.fatos(), fatos)
        self.assertEqual(len(fatos), 360)

    def test_rodar_depois_nao_acumula_dias(self):
        self.rodar("--fim", "2026-10-15")
        self.rodar("--fim", "2026-10-20")  # outro dia: troca a janela, não soma 60 dias
        self.assertEqual(len({f[0] for f in self.fatos()}), 30)

    def test_limpar_deixa_o_diretorio_como_estava(self):
        antes = arvore(self.d)
        self.rodar(*PORTA)
        self.assertNotEqual(arvore(self.d), antes)
        rc, _, _ = self.rodar("--limpar")
        self.assertEqual(rc, 0)
        self.assertEqual(arvore(self.d), antes)

    def test_limpar_preserva_dado_real_gravado_sobre_a_demo(self):
        self.rodar(*PORTA)
        negocio, fonte, metrica = "meu-site", "gsc", "cliques"
        with closing(armazem.abrir(self.d)) as c:
            dia, negocio, fonte, metrica = c.execute("select dia, negocio, fonte, metrica from fato where metrica='cliques' limit 1").fetchone()
            c.execute("update fato set valor=987654 where dia=? and negocio=? and fonte=? and metrica=?", (dia, negocio, fonte, metrica))
            c.commit()
        self.rodar("--limpar")
        self.assertEqual([(f[0], f[1], f[3], f[5]) for f in self.fatos()], [(dia, negocio, metrica, 987654.0)])

    def test_limpar_so_remove_o_marcado(self):
        self.rodar(*PORTA)
        real = metas.criar("LCP da home abaixo de 2,5 s", abrir=lambda m: "X-1", base_dados=self.d)
        armazem.gravar([{"dia": FIM, "negocio": "meu-site", "fonte": "gsc", "metrica": "cliques", "valor": 5}], self.d)
        (self.d / "projetos" / "meu-site.json").write_text("{}", encoding="utf-8")
        self.rodar("--limpar")
        self.assertEqual([m["id"] for m in metas.listar(self.d)], [real["id"]])
        self.assertEqual([f[1] for f in self.fatos()], ["meu-site"])
        self.assertTrue((self.d / "projetos" / "meu-site.json").is_file())
        self.assertFalse((self.d / "demo.json").exists())

    def test_limpar_sem_demo_nao_toca_em_nada(self):
        (self.d / "projetos").mkdir()
        (self.d / "projetos" / "meu-site.json").write_text("{}", encoding="utf-8")
        antes = arvore(self.d)
        rc, saida, _ = self.rodar("--limpar")
        self.assertEqual(rc, 0)
        self.assertIn("nada marcado", saida)
        self.assertEqual(arvore(self.d), antes)

    def test_recusa_sobre_dado_real(self):
        casos = {
            "projeto": lambda: ((self.d / "projetos").mkdir(), (self.d / "projetos" / "meu-site.json").write_text("{}")),
            "meta": lambda: metas.criar("Objetivo real da casa", abrir=lambda m: "X-1", base_dados=self.d),
            "fato": lambda: armazem.gravar([{"dia": FIM, "negocio": "meu-site", "fonte": "gsc", "metrica": "cliques", "valor": 1}], self.d),
            "proposta": lambda: propostas.criar("real", "email", "ana@exemplo.test", "Oi", "Texto", dados={"assunto": "Oi", "de": "Ana <ana@exemplo.test>"}, base_dados=self.d),
        }
        for nome, cria in casos.items():
            with self.subTest(nome):
                d = Path(self.tmp.name) / nome
                d.mkdir()
                self.d = d
                with mock.patch.dict(os.environ, {"HUB_DATA": str(d), "KRONOS_CAMINHOS_DADOS": str(d)}), mock.patch.object(sites, "PASTA", d / "projetos"):
                    cria()
                    antes = arvore(d)
                    rc, _, err = self.rodar("--dados", str(d), *PORTA)
                    self.assertEqual(rc, 1)
                    self.assertIn("recusei", err)
                    self.assertEqual(arvore(d), antes)
                    self.assertFalse((d / "demo.json").exists())

    def test_recusa_projeto_de_exemplo_sem_a_marca(self):
        """Mesmo um projeto idêntico ao de exemplos/ (copiado pelo init) é dado do dono: sem marca, recusa."""
        (self.d / "projetos").mkdir()
        (self.d / "projetos" / "exemplo-loja.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.rodar(*PORTA)[0], 1)

    def test_todas_as_telas_do_menu_respondem_200(self):
        self.rodar(*PORTA)
        caminhos = [c for _, _, itens in ui.menu() for c, _ in itens]
        self.assertGreater(len(caminhos), 10)
        for c in caminhos:
            with self.subTest(c):
                self.assertEqual(painel.rota(c)[0], 200)

    def test_faixa_aparece_com_demo_e_some_sem(self):
        self.assertNotIn('class="demo-faixa"', ui.pagina("T", "<p>x</p>"))
        self.assertFalse(ui.demo_ativa() or demo.ativa())
        self.rodar(*PORTA)
        self.assertTrue(ui.demo_ativa() and demo.ativa())
        for c in ("/metas", "/propostas", "/bi", "/pendencias"):
            with self.subTest(c):
                html = painel.rota(c)[1]
                self.assertIn('<div class="demo-faixa" role="status">Dados de demonstração', html)
        self.assertIn('class="demo-faixa"', ui.entrar())  # até a tela de entrada
        self.rodar("--limpar")
        self.assertNotIn('class="demo-faixa"', ui.pagina("T", "<p>x</p>"))

    def test_faixa_escapa_o_texto(self):
        with mock.patch.object(ui, "FAIXA_DEMO", '<script>alert("x")</script>'), mock.patch.object(ui, "demo_ativa", lambda: True):
            html = ui.pagina("T", "<p>x</p>")
        self.assertNotIn('<script>alert("x")', html)
        self.assertIn("&lt;script&gt;alert", html)

    def test_faixa_com_pasta_ausente_nao_quebra(self):
        with mock.patch.dict(os.environ, {"HUB_DATA": str(self.d / "nao-existe")}):
            self.assertFalse(ui.demo_ativa())
            self.assertNotIn('class="demo-faixa"', ui.pagina("T", "<p>x</p>"))

    def test_demo_visivel_na_tela_de_metas(self):
        self.rodar(*PORTA)
        self.assertIn("LCP da home da Loja Exemplo", painel.rota("/metas")[1])

    # ---------- revisão 1: limpar valida a marca, rollback, onde o painel lê ----------

    def test_limpar_com_marca_adulterada_nao_apaga_fora_da_pasta(self):
        self.rodar(*PORTA)
        fora = Path(self.tmp.name) / "fora"
        fora.mkdir()
        (fora / "x.json").write_text('{"demo": true}', encoding="utf-8")
        (self.d / "x.json").write_text('{"demo": true}', encoding="utf-8")
        (self.d / "projetos" / "meu-site.json").write_text("{}", encoding="utf-8")
        marca = json.loads((self.d / "demo.json").read_text(encoding="utf-8"))
        for chave in ("projetos", "metas", "propostas", "recibos"):
            marca[chave] += ["../../fora/x", "../x", str(fora / "x"), 7, None, ["a"], "..", "meu-site", "a/b"]
        (self.d / "demo.json").write_text(json.dumps(marca), encoding="utf-8")
        rc, saida, _ = self.rodar("--limpar")
        self.assertEqual(rc, 0)
        self.assertTrue((fora / "x.json").is_file())
        self.assertTrue((self.d / "x.json").is_file())
        self.assertTrue((self.d / "projetos" / "meu-site.json").is_file())  # id que não é dos exemplos: nunca
        self.assertIn("ignorado", saida)
        self.assertEqual(metas.listar(self.d), [])  # os ids de verdade saíram

    def test_limpar_com_listas_que_nao_sao_listas(self):
        self.rodar(*PORTA)
        (self.d / "demo.json").write_text(json.dumps({"demo": True, "projetos": "x", "metas": {"a": 1}, "negocios": 5, "de": 3}), encoding="utf-8")
        self.assertEqual(self.rodar("--limpar")[0], 0)

    def test_falha_no_meio_nao_prende_a_pasta(self):
        with mock.patch.object(metas, "gravar", side_effect=OSError("disco cheio")):
            with self.assertRaises(OSError):
                self.rodar(*PORTA)
        marca = json.loads((self.d / "demo.json").read_text(encoding="utf-8"))
        self.assertIs(marca["completo"], False)
        self.assertTrue(demo.ativa())
        self.assertEqual(self.rodar(*PORTA)[0], 0)  # de novo funciona
        self.assertIs(json.loads((self.d / "demo.json").read_text(encoding="utf-8"))["completo"], True)

    def test_falha_no_meio_e_limpar_deixa_como_estava(self):
        antes = arvore(self.d)
        with mock.patch.object(propostas, "_gravar", side_effect=OSError("x")):
            with self.assertRaises(OSError):
                self.rodar(*PORTA)
        self.assertNotEqual(arvore(self.d), antes)
        self.assertEqual(self.rodar("--limpar")[0], 0)
        self.assertEqual(arvore(self.d), antes)

    def test_provisorio_nao_libera_dado_real(self):
        with mock.patch.object(metas, "gravar", side_effect=OSError("x")):
            with self.assertRaises(OSError):
                self.rodar(*PORTA)
        (self.d / "projetos" / "meu-site.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.rodar(*PORTA)[0], 1)

    def test_config_projetos_resolve_para_a_pasta_da_demo(self):
        """Sem trocar sites.PASTA: com KRONOS_CAMINHOS_DADOS=<dir> e <dir>/projetos existindo, o painel lê <dir>/projetos."""
        from hub import config
        self.rodar(*PORTA)
        self.assertEqual(config.projetos().resolve(), (self.d / "projetos").resolve())
        self.assertEqual(demo.pasta_projetos_do_painel(self.d).resolve(), (self.d / "projetos").resolve())
        _, erros = zip(*[sites._ler(a) for a in sorted(config.projetos().glob("*.json"))])
        self.assertEqual([e for e in erros if e], [])

    def test_recusa_olha_onde_o_painel_le(self):
        real = Path(self.tmp.name) / "sites-reais"
        real.mkdir()
        (real / "meu-site.json").write_text("{}", encoding="utf-8")
        with mock.patch.dict(os.environ, {"KRONOS_PROJETOS": str(real)}):
            rc, _, err = self.rodar(*PORTA)
        self.assertEqual(rc, 1)
        self.assertIn(str(real), err)
        self.assertFalse((self.d / "demo.json").exists())

    def test_limpar_so_apaga_fato_do_intervalo_e_metricas_da_demo(self):
        self.rodar(*PORTA)
        extras = [{"dia": "2020-01-01", "negocio": "exemplo-loja", "fonte": "gsc", "metrica": "cliques", "valor": 9},
                  {"dia": FIM, "negocio": "exemplo-loja", "fonte": "gsc", "metrica": "outra", "valor": 9},
                  {"dia": FIM, "negocio": "exemplo-loja", "fonte": "gsc", "metrica": "cliques", "dim": "pais:br", "valor": 9}]
        armazem.gravar(extras, self.d)
        self.rodar("--limpar")
        self.assertEqual(len(self.fatos()), 3)
        self.assertTrue((self.d / "bi" / "metricas.db").is_file())  # banco com dado de outro: fica

    def test_readme_manda_reiniciar_o_painel(self):
        raiz = Path(__file__).resolve().parent.parent
        base = raiz / "publico" if (raiz / "publico").is_dir() else raiz  # no snapshot público o overlay publico/ vai para a raiz
        for rel in ("README.md", "README.pt-BR.md"):
            self.assertIn("docker compose restart painel", (base / rel).read_text(encoding="utf-8"))

    # ---------- revisão 2: re-run com outra data, demo.json forjado ----------

    def test_rerun_com_outro_fim_nao_deixa_orfaos(self):
        antes = arvore(self.d)
        self.rodar("--fim", "2026-10-01")
        self.rodar("--fim", "2026-10-10")
        self.assertEqual(len(metas.listar(self.d)), 3)  # troca, não soma
        self.assertEqual(len(propostas.listar(base_dados=self.d)), 2)
        self.assertEqual(len(self.fatos()), 360)
        self.assertEqual(self.rodar("--limpar")[0], 0)
        self.assertEqual(arvore(self.d), antes)
        self.assertEqual(self.rodar("--fim", "2026-10-12")[0], 0)  # e a próxima demo não recusa

    def test_demo_json_forjado_nao_apaga_nem_esconde_dado_real(self):
        (self.d / "projetos").mkdir()
        real = self.d / "projetos" / "exemplo-loja.json"
        real.write_text('{"nome": "meu projeto de verdade"}', encoding="utf-8")
        armazem.gravar([{"dia": "2026-10-01", "negocio": "real", "fonte": "gsc", "metrica": "cliques", "valor": 5}], self.d)
        m_real = metas.criar("Meta real da casa", abrir=lambda m: "X-1", base_dados=self.d)
        (self.d / "demo.json").write_text(json.dumps({"demo": True, "projetos": ["exemplo-loja"], "negocios": ["real"], "de": "0", "ate": "9",
                                                      "banco_novo": False, "metas": [m_real["id"]]}), encoding="utf-8")
        antes = arvore(self.d)
        sem_banco = lambda: {k: v for k, v in arvore(self.d).items() if k != "bi/metricas.db"}
        a = sem_banco()
        rc, _, err = self.rodar("--limpar")
        self.assertEqual(rc, 0)
        self.assertEqual(real.read_text(encoding="utf-8"), '{"nome": "meu projeto de verdade"}')
        self.assertEqual([f[1] for f in self.fatos()], ["real"])
        self.assertEqual([m["id"] for m in metas.listar(self.d)], [m_real["id"]])
        # a marca forjada também não esconde nada da recusa
        (self.d / "demo.json").write_text(json.dumps({"demo": True, "projetos": ["exemplo-loja"], "negocios": ["real"], "de": "0", "ate": "9",
                                                      "metas": [m_real["id"]]}), encoding="utf-8")
        rc, _, err = self.rodar(*PORTA)
        self.assertEqual(rc, 1)
        self.assertIn("recusei", err)
        self.assertEqual(sem_banco().keys() - a.keys(), set())

    def test_marca_com_datas_invertidas_ou_lixo_nao_apaga_fatos(self):
        self.rodar(*PORTA)
        for de, ate in (("2026-12-31", "2026-01-01"), ("x", "y"), (1, 2), (None, None)):
            m = json.loads((self.d / "demo.json").read_text(encoding="utf-8"))
            m["de"], m["ate"] = de, ate
            (self.d / "demo.json").write_text(json.dumps(m), encoding="utf-8")
            self.rodar("--limpar")
            self.assertEqual(len(self.fatos()), 360, (de, ate))
            (self.d / "demo.json").write_text(json.dumps({**m, "de": "2026-09-16", "ate": FIM}), encoding="utf-8")

    def test_id_de_meta_fora_do_padrao_da_demo_nao_e_apagado(self):
        self.rodar(*PORTA)
        m = metas.criar("Meta marcada demo mas de outro", abrir=lambda m: "X-1", base_dados=self.d)
        m["demo"] = True
        metas.gravar(m, self.d)
        marca = json.loads((self.d / "demo.json").read_text(encoding="utf-8"))
        marca["metas"].append(m["id"])
        (self.d / "demo.json").write_text(json.dumps(marca), encoding="utf-8")
        self.rodar("--limpar")
        self.assertEqual([x["id"] for x in metas.listar(self.d)], [m["id"]])


if __name__ == "__main__":
    unittest.main()
