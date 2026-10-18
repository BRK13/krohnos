"""Tela Plugins (/config/plugins): lista, versão e o que traz (sem importar plugin desligado), liga/desliga na sobreposição."""
import os
import sys
import tempfile
import textwrap
import tomllib
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from hub import config, config_telas, painel, plugins, plugins_tela, ui


_is_dir = Path.is_dir


def plugin(pasta, nome, corpo):
    d = Path(pasta) / f"kronos_plugin_{nome}"
    d.mkdir(parents=True)
    (d / "__init__.py").write_text(textwrap.dedent(corpo), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.pasta, self.dados = self.dir / "plugins", self.dir / "dados"
        self.dados.mkdir()
        self.marca = self.dir / "importado.txt"
        plugin(self.pasta, "bom", '''
            __version__ = "1.2.3"
            ROTAS = {"/bom": None, "/bom/<x>/y": None}
            MENU = [("Grupo", "/bom", "Bom <b>")]
            ROTINAS = [("rot-bom", "python3 -m hub.bom")]
        ''')
        plugin(self.pasta, "quebra", "raise RuntimeError('boom <i>x</i>')\n")
        plugin(self.pasta, "efeito", f'''
            __version__ = "0.9"
            ROTAS = {{"/efeito": None}}
            open({str(self.marca)!r}, "w").write("importou")
        ''')
        toml = self.dir / "k.toml"
        toml.write_text(f'[plugins]\ncaminho = "{self.pasta}"\n', encoding="utf-8")
        env = {"KRONOS_CONFIG": str(toml), "KRONOS_CAMINHOS_DADOS": str(self.dados)}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("KRONOS_PLUGINS_LIGADOS", None)  # tests/__init__.py o define; aqui vale a sobreposição
        for alvo in (mock.patch.object(plugins, "caminhos", lambda: [self.pasta]),  # só a pasta do teste (sem privado/plugins)
                     mock.patch.object(Path, "is_dir", lambda p: False if str(p) == config.LEGADO["caminhos.codigo"] else _is_dir(p))):  # sem legado da VPS
            alvo.start()
            self.addCleanup(alvo.stop)
        for n in ("bom", "quebra", "efeito"):
            self.addCleanup(sys.modules.pop, f"kronos_plugin_{n}", None)
        self.addCleanup(plugins.recarregar)
        self.addCleanup(config.recarregar)
        config.recarregar()
        plugins.recarregar()
        self.csrf = painel._csrf()

    def liga(self, lista):
        config.gravar({"plugins.ligados": lista})
        plugins.recarregar()

    def post(self, campos, csrf_ok=True):
        return config_telas.tratar_post("/config/plugins", {"csrf": self.csrf, **campos}, "ana", "1.2.3.4", csrf_ok)

    def pagina(self):
        return config_telas.ROTAS_GET["/config/plugins"]({}, "ana", self.csrf)

    def gravado(self):
        with open(config.sobreposicao(), "rb") as f:
            return tomllib.load(f)["plugins"]["ligados"]


class ListaTest(Base):
    def test_lista_sem_importar_desligado(self):
        st, h = self.pagina()
        self.assertEqual(st, 200)
        for n in ("bom", "quebra", "efeito"):
            self.assertIn(f'<b class="mono">{n}</b>', h)
        self.assertIn("1.2.3", h)  # versão lida por AST
        self.assertIn("0.9", h)
        self.assertIn("/bom/&lt;x&gt;/y", h)  # extração estática dos desligados
        self.assertIn("rot-bom", h)
        self.assertFalse(self.marca.exists(), "plugin desligado foi importado")
        self.assertNotIn("kronos_plugin_efeito", sys.modules)

    def test_ligado_mostra_o_carregado_e_erro(self):
        self.liga(["bom", "quebra"])
        _, h = self.pagina()
        self.assertIn("RuntimeError", h)
        self.assertIn("&lt;i&gt;x&lt;/i&gt;", h)  # erro escapado
        self.assertNotIn("<i>x</i>", h)
        self.assertIn("Bom &lt;b&gt;", h)  # menu escapado
        self.assertNotIn("Bom <b>", h)
        self.assertIn("/bom/&lt;x&gt;/y", h)
        self.assertFalse(self.marca.exists())

    def test_aviso_de_confianca(self):
        self.assertIn("executa o código", self.pagina()[1])

    def test_cartao_whatsapp_desligado_por_padrao(self):
        _, h = self.pagina()
        self.assertIn("Plugin externo: WhatsApp (triagem-wpp)", h)
        self.assertIn(f"https://github.com/{config.get('github.org')}/triagem-wpp", h)
        self.assertIn("projeto separado", h)
        self.assertIn("desligado", h)
        self.assertNotIn("triagem_wpp", sys.modules)

    def test_whatsapp_sem_org_nao_quebra(self):
        with mock.patch.dict(os.environ, {"KRONOS_GITHUB_ORG": ""}):
            config.recarregar()
            st, h = self.pagina()
        self.assertEqual(st, 200)
        self.assertIn("defina github.org", h)  # texto de fallback, sem link
        self.assertNotIn("https://github.com/", h)

    def test_sem_plugins_achados(self):
        with mock.patch.object(plugins, "descobertos", return_value={}):
            st, h = self.pagina()
        self.assertEqual(st, 200)
        self.assertIn("Nenhum plugin", h)

    def test_menu_configurar_tem_plugins(self):
        itens = next(i for g, _, i in ui.menu() if g == "Configurar")
        self.assertIn(("/config/plugins", "Plugins"), itens)


class EstaticoTest(unittest.TestCase):
    def test_arquivo_ilegivel_ou_dinamico_nao_levanta(self):
        with tempfile.TemporaryDirectory() as t:
            plugin(t, "a", "def (:\n")
            plugin(t, "b", "__version__ = open('x').read()\nROTAS = dict(a=1)\nMENU = f()\n")
            for n in "ab":
                info = plugins_tela.ler_estatico(Path(t) / f"kronos_plugin_{n}")
                self.assertIsNone(info["versao"])


class LigarTest(Base):
    def test_liga_grava_e_recarrega(self):
        with mock.patch.object(plugins, "recarregar", wraps=plugins.recarregar) as rec:
            st, dest = self.post({"acao": "ligar", "nome": "bom"})
        self.assertEqual((st, dest.split("?")[0]), (303, "/config/plugins"))
        self.assertIn("ok=", dest)
        self.assertEqual(self.gravado(), ["bom"])
        rec.assert_called()
        self.assertEqual(plugins.ligados(), ["bom"])
        self.assertFalse(self.marca.exists())

    def test_desliga(self):
        self.liga(["bom", "efeito"])
        self.post({"acao": "desligar", "nome": "bom"})
        self.assertEqual(self.gravado(), ["efeito"])

    def test_curinga_materializado_ao_desligar(self):
        self.liga(["*"])
        self.post({"acao": "desligar", "nome": "quebra"})
        self.assertEqual(self.gravado(), ["bom", "efeito"])
        self.assertEqual(sorted(plugins.ligados()), ["bom", "efeito"])

    def test_curinga_ligar_nao_perde_nada(self):
        self.liga(["*"])
        self.post({"acao": "ligar", "nome": "bom"})
        self.assertEqual(sorted(plugins.ligados()), ["bom", "efeito", "quebra"])

    def test_nome_fora_dos_descobertos_recusado(self):
        for nome in ("naoexiste", "../x", "", "os", "kronos_plugin_bom/../x"):
            st, dest = self.post({"acao": "ligar", "nome": nome})
            self.assertEqual(st, 303)
            self.assertIn("erro=", dest, nome)
        self.assertFalse(config.sobreposicao().exists())

    def test_prefixo_no_nome_vale(self):
        self.post({"acao": "ligar", "nome": "kronos_plugin_bom"})
        self.assertEqual(self.gravado(), ["bom"])

    def test_sem_csrf_403_e_nada_gravado(self):
        st, _ = self.post({"acao": "ligar", "nome": "bom"}, csrf_ok=False)
        self.assertEqual(st, 403)
        self.assertFalse(config.sobreposicao().exists())

    def test_acao_desconhecida(self):
        self.assertIn("erro=", self.post({"acao": "x", "nome": "bom"})[1])

    def test_ligar_nao_importa_na_gravacao_so_na_carga(self):
        self.post({"acao": "ligar", "nome": "efeito"})
        self.assertFalse(self.marca.exists())  # a tela não importa; o painel o faz ao carregar (aviso na tela)
        plugins.rotas()
        self.assertTrue(self.marca.exists())

    def test_variavel_de_ambiente_vence_e_nada_e_gravado(self):
        self.liga(["bom"])
        antes = config.sobreposicao().read_bytes()
        with mock.patch.dict(os.environ, {"KRONOS_PLUGINS_LIGADOS": '["*"]'}):
            config.recarregar()
            with mock.patch.object(config, "gravar", side_effect=AssertionError("gravou")):
                _, dest = self.post({"acao": "desligar", "nome": "bom"})
        self.assertIn("erro=", dest)
        self.assertIn("KRONOS_PLUGINS_LIGADOS", dest)
        self.assertEqual(config.sobreposicao().read_bytes(), antes)

    def test_materializar_curinga_avisa_sobre_plugins_novos(self):
        self.liga(["*"])
        _, dest = self.post({"acao": "desligar", "nome": "quebra"})
        msg = urllib.parse.unquote(dest)
        self.assertIn("ligados à mão", msg)
        self.assertIn("plugins novos", msg)

    def test_forms_levam_csrf(self):
        _, h = self.pagina()
        self.assertIn(f'name="csrf" value="{self.csrf}"', h)
        self.assertIn('action="/config/plugins"', h)


if __name__ == "__main__":
    unittest.main()
