import json
import datetime as dt
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

import hub
from hub import cofre, conexoes, conexoes_tela, config, config_telas, limite, painel, sites, ui, usuarios, verificar_fontes
from tests import sessao_teste

VALOR = "sk-valor-secreto-ABCD1234wxyz"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(cofre, "_pasta", lambda: Path(self.tmp.name)),
                  mock.patch.dict(os.environ, {"KROHNOS_CHAVE_MESTRA": Fernet.generate_key().decode(), "HUB_DATA": self.tmp.name,
                                               "PAINEL_SENHA": ""})):
            p.start()
            self.addCleanup(p.stop)

    def csrf(self):
        return "csrf-de-teste"


class CofreHtmlTest(Base):
    def test_lista_sem_valor_e_com_fim4(self):
        cofre.definir("linear.token", VALOR, por="ana")
        st, corpo = config_telas.cofre_get({}, "ana", self.csrf())
        self.assertEqual(st, 200)
        self.assertIn("linear.token", corpo)
        self.assertIn("••••wxyz", corpo)
        self.assertIn("ana", corpo)
        self.assertIn("Trocar", corpo)
        self.assertIn("Apagar", corpo)
        self.assertNotIn(VALOR, corpo)
        self.assertNotIn(VALOR[:-4], corpo)
        for pedaco in (VALOR, VALOR[:-4], VALOR[3:], "ABCD1234"):
            self.assertNotIn(pedaco, corpo)
        corpo_sem_csrf = re.sub(r'<input[^>]*name="csrf"[^>]*>', '', corpo)  # o token CSRF é hex aleatório: pode conter 1234/ABCD
        self.assertNotRegex(corpo_sem_csrf, r'(value|data-[a-z-]+)="[^"]*(ABCD|1234|secreto)')
        self.assertNotRegex(corpo, r'data-[a-z-]+="[^"]*wxyz')  # fim4 só no texto da célula, nunca em atributo
        self.assertIn('type="password"', corpo)
        self.assertIn('autocomplete="off"', corpo)
        self.assertIn(f'name="csrf" value="{self.csrf()}"', corpo)

    def test_valor_curto_sem_fim4(self):
        cofre.definir("a.b", "curto", por="ana")
        _, corpo = config_telas.cofre_get({}, "ana", self.csrf())
        self.assertIn("••••", corpo)
        self.assertNotIn("curto", corpo)

    def test_vazio_e_escape(self):
        _, corpo = config_telas.cofre_get({}, "ana", self.csrf())
        self.assertIn("Nenhum segredo", corpo)
        cofre.definir("x.y", VALOR, por="<img src=x onerror=1>")
        _, corpo = config_telas.cofre_get({}, "<b>", self.csrf())
        self.assertNotIn("<img src=x", corpo)
        self.assertIn("&lt;img", corpo)

    def test_aba_auditoria(self):
        cofre.definir("x.y", VALOR, por="ana", ip="10.0.0.9")
        cofre.apagar("x.y", por="bia")
        _, corpo = config_telas.cofre_get({"aba": "auditoria"}, "ana", self.csrf())
        self.assertIn("definir", corpo)
        self.assertIn("apagar", corpo)
        self.assertIn("10.0.0.9", corpo)
        self.assertIn("bia", corpo)
        self.assertNotIn(VALOR, corpo)
        self.assertIn("usuário informado no acesso", corpo)

    def test_auditoria_limitada_a_200(self):
        with mock.patch.object(cofre, "auditoria", return_value=[]) as m:
            config_telas.cofre_get({"aba": "auditoria"}, "ana", self.csrf())
        m.assert_called_once_with(200)

    def test_sem_permissao_nao_ve_botoes(self):
        cofre.definir("x.y", VALOR, por="ana")
        with mock.patch.object(config_telas, "pode", lambda u, a: False):
            _, corpo = config_telas.cofre_get({}, "ana", self.csrf())
        self.assertNotIn("Apagar", corpo)
        self.assertNotIn('type="password"', corpo)

    def test_pode_hoje_e_sempre_true(self):
        self.assertTrue(config_telas.pode("qualquer", "cofre.definir"))


class CofrePostTest(Base):
    def test_registro_de_rotas(self):
        self.assertIn("/config/cofre", config_telas.ROTAS_GET)
        self.assertIn("/config/cofre", config_telas.ROTAS_POST)

    def post(self, campos, csrf=True, usuario="ana", ip="1.2.3.4"):
        return config_telas.tratar_post("/config/cofre", campos, usuario, ip, csrf)

    def test_sem_csrf_403_e_nada_grava(self):
        st, destino = self.post({"acao": "definir", "nome": "a.b", "valor": VALOR}, csrf=False)
        self.assertEqual(st, 403)
        self.assertEqual(cofre.listar(), [])

    def test_grava_e_audita(self):
        st, destino = self.post({"acao": "definir", "nome": "linear.token", "valor": VALOR})
        self.assertEqual(st, 303)
        self.assertTrue(destino.startswith("/config/cofre?ok="))
        self.assertNotIn(VALOR, urllib.parse.unquote(destino))
        self.assertEqual(cofre.ler("linear.token"), VALOR)
        a = cofre.auditoria()[0]
        self.assertEqual((a["quem"], a["acao"], a["nome"], a["ip"]), ("ana", "definir", "linear.token", "1.2.3.4"))
        self.assertEqual(cofre.listar()[0]["por"], "ana")

    def test_trocar_e_apagar(self):
        self.post({"acao": "definir", "nome": "a.b", "valor": VALOR})
        self.post({"acao": "definir", "nome": "a.b", "valor": VALOR[::-1]}, usuario="bia")
        self.assertEqual(cofre.listar()[0]["por"], "bia")
        st, destino = self.post({"acao": "apagar", "nome": "a.b"}, usuario="bia", ip="192.0.2.4")
        self.assertEqual(st, 303)
        self.assertIn("ok=", destino)
        self.assertEqual(cofre.listar(), [])
        a = cofre.auditoria()[0]
        self.assertEqual((a["quem"], a["acao"], a["ip"]), ("bia", "apagar", "192.0.2.4"))

    def test_apagar_inexistente_erro(self):
        _, destino = self.post({"acao": "apagar", "nome": "nao.existe"})
        self.assertIn("erro=", destino)

    def test_nome_invalido_erro_em_portugues_sem_valor(self):
        st, destino = self.post({"acao": "definir", "nome": "nome ruim!", "valor": VALOR})
        self.assertEqual(st, 303)
        msg = urllib.parse.unquote_plus(destino.split("erro=")[1])
        self.assertIn("nome inválido", msg)
        self.assertNotIn(VALOR, destino)
        self.assertEqual(cofre.listar(), [])

    def test_valor_vazio_e_acao_desconhecida(self):
        _, d = self.post({"acao": "definir", "nome": "a.b", "valor": ""})
        self.assertIn("erro=", d)
        _, d = self.post({"acao": "explodir", "nome": "a.b"})
        self.assertIn("erro=", d)

    def test_erro_inesperado_nao_vaza_valor(self):
        with mock.patch.object(cofre, "definir", side_effect=OSError(f"falhou {VALOR}")):
            _, destino = self.post({"acao": "definir", "nome": "a.b", "valor": VALOR})
        self.assertNotIn(VALOR, urllib.parse.unquote_plus(destino))

    def test_sem_permissao_403(self):
        with mock.patch.object(config_telas, "pode", lambda u, a: False):
            st, _ = self.post({"acao": "definir", "nome": "a.b", "valor": VALOR})
        self.assertEqual(st, 403)
        self.assertEqual(cofre.listar(), [])

    def test_rota_desconhecida_404(self):
        self.assertEqual(config_telas.tratar_post("/config/nada", {}, "ana", "", True)[0], 404)


HOSTIL = '<img src=x onerror=alert(1)>"&'


class ProjetosBase(Base):
    def setUp(self):
        super().setUp()
        self.pasta = Path(self.tmp.name) / "projetos"
        self.pasta.mkdir()
        for p in (mock.patch.object(sites, "PASTA", self.pasta),
                  mock.patch.object(verificar_fontes, "_pasta", lambda: Path(self.tmp.name) / "verificacoes"),
                  mock.patch.dict(config_telas.CLIENTES, {}, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def campos(self, **extra):
        c = {"csrf": self.csrf(), "acao": "salvar", "modo": "criar", "nome": "Loja Exemplo", "id": "loja-exemplo",
             "dominio": "exemplo-loja.test", "repo_github": "dono/exemplo-loja", "repo_base": "main", "repo_pacotes": "yarn",
             "repo_deploy": "dokploy", "gsc": "sc-domain:exemplo-loja.test", "ga4": "123456789", "ga4_medicao": "G-ABCDEF1234",
             "clarity": "abcdef1234", "gmb_cid": "", "concorrentes": "rival-um.test\r\nrival-dois.test\r\n", "conselho": "",
             "marca_nome": "", "aliases": "Loja\r\nLojinha", "contexto": "Loja de exemplo.", "ativo": "1"}
        return {**c, **extra}

    def post(self, campos, csrf=True):
        return config_telas.tratar_post("/config/projetos", campos, "ana", "1.2.3.4", csrf)

    def get(self, q=None):
        return config_telas.projetos_get(q or {}, "ana", self.csrf())[1]


class ProjetosPostTest(ProjetosBase):
    def test_rotas_e_menu(self):
        self.assertIn("/config/projetos", config_telas.ROTAS_GET)
        self.assertIn("/config/projetos", config_telas.ROTAS_POST)
        from hub import ui
        self.assertIn("/config/projetos", [c for g, _, itens in ui.MENU if g == "Configurar" for c, _ in itens])

    def test_criar_gera_json_valido(self):
        st, destino = self.post(self.campos())
        self.assertEqual((st, destino.split("?")[0]), (303, "/config/projetos"))
        self.assertIn("ok=", destino)
        s = json.loads((self.pasta / "loja-exemplo.json").read_text(encoding="utf-8"))
        self.assertEqual(sites.validar(s), [])
        self.assertEqual(s["concorrentes"], ["rival-um.test", "rival-dois.test"])
        self.assertEqual(s["marca"]["aliases"], ["Loja", "Lojinha"])
        self.assertEqual(s["marca"]["nome"], "Loja Exemplo")
        self.assertEqual(s["repo"], {"github": "dono/exemplo-loja", "base": "main", "pacotes": "yarn", "deploy": "dokploy"})
        self.assertNotIn("gmb_cid", s["ids"])
        self.assertIsNone(s["conselho"])
        self.assertTrue(s["ativo"])

    def test_sem_csrf_403_e_nada_grava(self):
        st, _ = self.post(self.campos(), csrf=False)
        self.assertEqual(st, 403)
        self.assertEqual(list(self.pasta.iterdir()), [])

    def test_id_repetido_erro_no_campo(self):
        self.post(self.campos())
        st, corpo = self.post(self.campos(nome="Outra", dominio="outro-exemplo.test"))
        self.assertEqual(st, 200)  # volta o formulário, não redireciona
        self.assertRegex(corpo, r'(?s)name="id".*?já existe um projeto')
        self.assertIn("Outra", corpo)  # o que a pessoa digitou fica
        self.assertEqual(json.loads((self.pasta / "loja-exemplo.json").read_text(encoding="utf-8"))["nome"], "Loja Exemplo")

    def test_id_malicioso_recusado(self):
        for ruim in ("../fora", "a/b", "..", "ID Maiusculo", ""):
            st, corpo = self.post(self.campos(id=ruim))
            self.assertEqual(st, 200, ruim)
            self.assertIn("minúsculas", corpo)
        self.assertEqual(list(self.pasta.iterdir()), [])
        self.assertFalse((Path(self.tmp.name) / "fora.json").exists())

    def test_erro_de_formato_volta_no_campo_escapado(self):
        st, corpo = self.post(self.campos(dominio=HOSTIL, ga4="abc"))
        self.assertEqual(st, 200)
        self.assertNotIn("<img src=x", corpo)
        self.assertIn("&lt;img src=x", corpo)
        bloco = re.search(r'<label class="campo"><span class="mu">GA4 \(ID da propriedade\)</span>.*?</label>', corpo, re.S).group(0)
        self.assertIn("formato inválido", bloco)  # dentro do bloco do campo, não só no banner
        banner = corpo.split("<form", 1)[0].split("</header>")[-1]
        self.assertNotIn("ids.ga4", banner)
        self.assertEqual(list(self.pasta.iterdir()), [])

    def test_editar_preserva_campos_que_o_formulario_nao_cobre(self):
        self.post(self.campos())
        arq = self.pasta / "loja-exemplo.json"
        s = json.loads(arq.read_text(encoding="utf-8"))
        s["ranking"] = {"palavras": ["x"], "local": 2076, "idioma": "pt", "dispositivo": "mobile", "profundidade": 20}
        s["hosts_alternativos"] = ["www.exemplo-loja.test"]
        s["ids"]["dokploy_app"] = "app-123"
        s["repo"]["extra"] = 1
        s["marca"]["identificador"] = "loja"
        s["padrao"]["paginas_amostra"] = ["/"]
        arq.write_text(json.dumps(s), encoding="utf-8")
        st, destino = self.post(self.campos(modo="editar", nome="Loja Nova", id="loja-exemplo", gsc="", ativo=""))
        self.assertEqual(st, 303, destino)
        n = json.loads(arq.read_text(encoding="utf-8"))
        self.assertEqual(n["nome"], "Loja Nova")
        self.assertFalse(n["ativo"])
        self.assertNotIn("gsc", n["ids"])
        for k in ("ranking", "hosts_alternativos", "padrao"):
            self.assertEqual(n[k], s[k], k)
        self.assertEqual(n["ids"]["dokploy_app"], "app-123")
        self.assertEqual(n["repo"]["extra"], 1)
        self.assertEqual(n["marca"]["identificador"], "loja")

    def test_editar_nao_troca_id_nem_cria_arquivo_novo(self):
        st, destino = self.post(self.campos(modo="editar", id="nao-existe"))
        self.assertEqual(st, 303)
        self.assertIn("erro=", destino)
        self.assertEqual(list(self.pasta.iterdir()), [])


class ProjetosHtmlTest(ProjetosBase):
    def test_lista_vazia(self):
        self.assertIn("Nenhum projeto", self.get())

    def test_nome_hostil_escapado_na_lista_e_no_form(self):
        self.post(self.campos())
        s = json.loads((self.pasta / "loja-exemplo.json").read_text(encoding="utf-8"))
        s["nome"] = HOSTIL
        s["contexto"] = HOSTIL
        (self.pasta / "loja-exemplo.json").write_text(json.dumps(s), encoding="utf-8")
        verificar_fontes._gravar("loja-exemplo", "deploy", "erro", HOSTIL, "2026-10-17T10:00:00+00:00", "exemplo-loja.test")
        for corpo in (self.get(), self.get({"editar": "loja-exemplo"})):
            self.assertNotIn("<img src=x", corpo)
            self.assertIn("&lt;img src=x", corpo)

    def test_selos_por_fonte(self):
        self.post(self.campos())
        verificar_fontes._gravar("loja-exemplo", "deploy", "ok", "Deploy ok.", "2026-10-17T10:00:00+00:00", "exemplo-loja.test")
        verificar_fontes._gravar("loja-exemplo", "repo", "erro", "Repositório: sumiu.", "2026-10-17T10:00:00+00:00", "dono/exemplo-loja")
        corpo = self.get()
        self.assertRegex(corpo, r'selo ok[^>]*>verificado 17/10')
        self.assertRegex(corpo, r'selo ruim[^>]*>erro')
        self.assertIn("Repositório: sumiu.", corpo)
        self.assertRegex(corpo, r'selo alerta[^>]*>não verificado')
        for nome in ("Search Console", "GA4", "Clarity", "PageSpeed", "Repositório", "Deploy"):
            self.assertIn(nome, corpo)
        self.assertIn(f'name="csrf" value="{self.csrf()}"', corpo)

    def test_verificacao_corrompida_nao_derruba(self):
        self.post(self.campos())
        pasta = Path(self.tmp.name) / "verificacoes"
        pasta.mkdir()
        (pasta / "loja-exemplo.json").write_text("{quebrado", encoding="utf-8")
        self.assertIn("não verificado", self.get())

    def test_formulario_novo_e_edicao(self):
        corpo = self.get({"novo": "1"})
        for nome in ("nome", "id", "dominio", "repo_github", "repo_base", "repo_pacotes", "repo_deploy", "gsc", "ga4", "ga4_medicao",
                     "clarity", "gmb_cid", "concorrentes", "conselho", "marca_nome", "aliases", "contexto", "ativo"):
            self.assertIn(f'name="{nome}"', corpo, nome)
        self.assertIn(f'name="csrf" value="{self.csrf()}"', corpo)
        self.post(self.campos())
        ed = self.get({"editar": "loja-exemplo"})
        self.assertIn('value="Loja Exemplo"', ed)
        self.assertIn("rival-um.test", ed)
        self.assertRegex(ed, r'name="id"[^>]*readonly')

    def test_editar_inexistente_ou_malicioso(self):
        self.assertIn("não existe", self.get({"editar": "nao-existe"}))
        self.assertIn("não existe", self.get({"editar": "../x"}))


class ProjetosFixTest(ProjetosBase):
    def lista(self):
        return self.get()

    def test_id_mudou_volta_a_ambar(self):
        self.post(self.campos())
        verificar_fontes._gravar("loja-exemplo", "ga4", "ok", "GA4 ok.", "2026-10-17T10:00:00+00:00", "123456789")
        self.assertRegex(self.lista(), r"selo ok[^>]*>verificado 17/10")
        self.post(self.campos(modo="editar", ga4="999999999"))
        corpo = self.lista()
        self.assertNotRegex(corpo, r"selo ok[^>]*>verificado")
        self.assertNotIn("GA4 ok.", corpo)

    def test_fonte_sem_id_e_nao_configurada(self):
        self.post(self.campos(gmb_cid=""))
        corpo = self.lista()
        self.assertRegex(corpo, r"selo alerta[^>]*>não configurado")
        self.assertNotRegex(corpo, r'value="gmb"')  # sem ID não há o que verificar

    def test_gmb_manual_e_ambar(self):
        self.post(self.campos(gmb_cid="12345678901"))
        _, destino = self.post({"csrf": self.csrf(), "acao": "verificar", "id": "loja-exemplo", "fonte": "gmb"})
        self.assertEqual(verificar_fontes.ler("loja-exemplo")["gmb"]["status"], "manual")
        self.assertRegex(self.lista(), r"selo alerta[^>]*>conferir à mão")
        self.assertIn("confirme no Perfil da Empresa", self.lista())

    def test_limpar_repo_remove_chave_e_reclama_no_campo(self):
        self.post(self.campos())
        st, corpo = self.post(self.campos(modo="editar", repo_base=""))
        self.assertEqual(st, 200)
        bloco = re.search(r'<label class="campo"><span class="mu">Branch base</span>.*?</label>', corpo, re.S).group(0)
        self.assertIn("obrigatório", bloco)
        st, destino = self.post(self.campos(modo="editar", repo_github="", repo_base="", repo_pacotes="", repo_deploy=""))
        self.assertEqual(st, 303)
        self.assertIsNone(json.loads((self.pasta / "loja-exemplo.json").read_text(encoding="utf-8"))["repo"])

    def test_contexto_vazio_no_campo(self):
        st, corpo = self.post(self.campos(contexto="   "))
        self.assertEqual(st, 200)
        bloco = re.search(r'<label class="campo cheio"><span class="mu">Contexto.*?</label>', corpo, re.S).group(0)
        self.assertIn("vazio", bloco)

    def test_json_corrompido_na_lista_nao_derruba(self):
        self.post(self.campos())
        (self.pasta / "quebrado.json").write_text("{nao e json", encoding="utf-8")
        corpo = self.lista()
        self.assertIn("quebrado", corpo)
        self.assertIn("Loja Exemplo", corpo)

    def test_editar_sem_mudar_nada_mantem_o_json_e_a_ordem_das_chaves(self):
        reais = sorted((Path(__file__).resolve().parent / "fixtures" / "sites_neutros").glob("*.json"))  # fixtures neutras (.test), sem depender de sites/
        self.assertGreaterEqual(len(reais), 7)
        for arq in reais:
            copia = self.pasta / arq.name
            copia.write_bytes(arq.read_bytes())
            antes = json.loads(copia.read_text(encoding="utf-8"))
            valores = config_telas._valores(antes)
            for quebra in ("\n", "\r\n"):  # o navegador manda \r\n nas textareas
                form = {k: (v.replace("\n", quebra) if isinstance(v, str) else v) for k, v in valores.items()}
                st, destino = self.post({"csrf": self.csrf(), "acao": "salvar", "modo": "editar", **form})
                self.assertEqual(st, 303, f"{arq.name}: {destino}")
                depois = json.loads(copia.read_text(encoding="utf-8"))
                self.assertEqual(depois, antes, arq.name)
                self.assertEqual(json.dumps(depois, ensure_ascii=False), json.dumps(antes, ensure_ascii=False), arq.name + ": ordem das chaves")


class ProjetosVerificarTest(ProjetosBase):
    def test_verificar_com_cliente_falso_grava(self):
        self.post(self.campos())
        config_telas.CLIENTES["deploy"] = lambda url, headers=None: (200, "")
        st, destino = self.post({"csrf": self.csrf(), "acao": "verificar", "id": "loja-exemplo", "fonte": "deploy"})
        self.assertEqual(st, 303)
        self.assertIn("ok=", destino)
        d = verificar_fontes.ler("loja-exemplo")["deploy"]
        self.assertEqual(d["status"], "ok")
        self.assertIn("200", d["mensagem"])

    def test_verificar_erro_aparece_na_lista(self):
        self.post(self.campos())
        config_telas.CLIENTES["deploy"] = lambda url, headers=None: (502, "")
        _, destino = self.post({"csrf": self.csrf(), "acao": "verificar", "id": "loja-exemplo", "fonte": "deploy"})
        self.assertIn("erro=", destino)
        self.assertIn("HTTP 502", self.get())

    def test_verificar_sem_csrf_403(self):
        self.post(self.campos())
        config_telas.CLIENTES["deploy"] = Mock = mock.Mock(return_value=(200, ""))
        st, _ = self.post({"csrf": "x", "acao": "verificar", "id": "loja-exemplo", "fonte": "deploy"}, csrf=False)
        self.assertEqual(st, 403)
        Mock.assert_not_called()

    def test_verificar_fonte_ou_id_ruim(self):
        self.post(self.campos())
        for c in ({"id": "loja-exemplo", "fonte": "orkut"}, {"id": "../x", "fonte": "deploy"}, {"id": "nao-existe", "fonte": "deploy"}):
            _, destino = self.post({"csrf": self.csrf(), "acao": "verificar", **c})
            self.assertIn("erro=", destino)


class ConexoesTelaTest(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(hub, "data", lambda: Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(conexoes_tela.TRANSPORTES.clear)

    def get(self):
        st, corpo = config_telas.conexoes_tela.conexoes_get({}, "ana", self.csrf())
        self.assertEqual(st, 200)
        return corpo

    def post(self, campos, csrf=True):
        return config_telas.tratar_post("/config/conexoes", campos, "ana", "10.0.0.1", csrf)

    def test_um_cartao_por_conexao_com_passos_e_campos_seguros(self):
        corpo = self.get()
        for nome, d in conexoes.CONEXOES.items():
            self.assertIn(f"id=c-{nome}", corpo)
            self.assertIn(d["titulo"], corpo)
        self.assertIn("Onde pegar", corpo)
        self.assertIn("paga, desligada por padrão", corpo)
        self.assertIn("em breve", corpo)
        self.assertIn('type="password"', corpo)
        self.assertIn('autocomplete="off"', corpo)
        self.assertIn('href="/config/conexoes" aria-current=page', corpo)
        self.assertIn("Testar", corpo)

    def test_html_nunca_tem_valores_so_fim4_e_quando(self):
        cofre.definir("linear.token", VALOR, por="ana")
        cofre.definir("dataforseo.senha", VALOR + "x", por="bia")
        conta = {"type": "service_account", "client_email": "user6@example.com",
                 "private_key": "-----BEGIN PRIVATE KEY-----\nSEGREDOPRIVADO\n-----END PRIVATE KEY-----\n"}
        cofre.definir("google.conta_servico", json.dumps(conta, separators=(",", ":")), por="ana")
        corpo = self.get()
        sem_csrf = re.sub(r'<input[^>]*name="csrf"[^>]*>', "", corpo)
        for pedaco in (VALOR, VALOR[:-4], "ABCD1234", "SEGREDOPRIVADO", "PRIVATE KEY"):
            self.assertNotIn(pedaco, sem_csrf)
        self.assertIn("••••wxyz", corpo)
        self.assertIn("por ana", corpo)
        self.assertIn("por bia", corpo)
        self.assertIn(conta["client_email"], corpo)
        self.assertIn(f'data-copiar="{conta["client_email"]}"', corpo)
        self.assertIn("Search Console", corpo)
        self.assertIn("Visualizador", corpo)

    def test_post_sem_csrf_403_e_nao_grava(self):
        st, _ = self.post({"acao": "salvar", "conexao": "linear", "linear.token": VALOR}, csrf=False)
        self.assertEqual(st, 403)
        self.assertEqual(cofre.listar(), [])

    def test_salvar_grava_no_cofre_com_auditoria_e_nao_ecoa_valor(self):
        st, destino = self.post({"csrf": self.csrf(), "acao": "salvar", "conexao": "linear", "linear.token": VALOR, "linear.time": "ENG"})
        self.assertEqual(st, 303)
        self.assertIn("ok=", destino)
        self.assertNotIn("sk-valor", destino)
        self.assertEqual(cofre.ler("linear.token"), VALOR)
        a = cofre.auditoria()[0]
        self.assertEqual((a["acao"], a["nome"], a["quem"], a["ip"]), ("definir", "linear.token", "ana", "10.0.0.1"))
        corpo = self.get()
        self.assertIn('value="ENG"', corpo)
        self.assertNotIn(VALOR, corpo)

    def test_google_json_invalido_recusado_em_portugues(self):
        for ruim in ("nao e json " + VALOR, json.dumps({"type": "service_account"})):
            st, destino = self.post({"csrf": self.csrf(), "acao": "salvar", "conexao": "google", "google.conta_servico": ruim})
            self.assertEqual(st, 303)
            self.assertIn("erro=", destino)
            self.assertNotIn("sk-valor", urllib.parse.unquote(destino))
        self.assertEqual(cofre.listar(), [])

    def test_testar_usa_transporte_injetado_e_mostra_o_ultimo_teste(self):
        cofre.definir("bing.chave", VALOR, por="ana")
        conexoes_tela.TRANSPORTES["bing"] = lambda *a, **k: (401, "")
        st, destino = self.post({"csrf": self.csrf(), "acao": "testar", "conexao": "bing"})
        self.assertEqual(st, 303)
        self.assertIn("erro=", destino)
        self.assertIn("401", urllib.parse.unquote(destino))
        corpo = self.get()
        self.assertIn("recusou a credencial (401)", corpo)
        self.assertIn("erro ", corpo)
        conexoes_tela.TRANSPORTES["bing"] = lambda *a, **k: (200, '{"d": []}')
        _, destino = self.post({"csrf": self.csrf(), "acao": "testar", "conexao": "bing"})
        self.assertIn("ok=", destino)
        self.assertIn("chave aceita", self.get())

    def test_org_e_projeto_escapados_na_tela(self):
        conexoes.guardar_campo("github.org", "<b>x</b>")
        conexoes.guardar_campo("clarity.projeto", '"><script>1</script>')
        corpo = self.get()
        self.assertNotIn("<b>x</b>", corpo)
        self.assertNotIn("<script>1</script>", corpo)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", corpo)
        self.assertIn("&lt;script&gt;1&lt;/script&gt;", corpo)

    def test_clarity_explica_token_por_projeto(self):
        corpo = self.get()
        self.assertIn("credenciais/clarity/", corpo)
        self.assertIn("Id do projeto", corpo)

    def test_conexao_ou_acao_ruim(self):
        for c in ({"conexao": "nada", "acao": "testar"}, {"conexao": "linear", "acao": "x"}, {"conexao": "linear_oauth", "acao": "testar"}):
            _, destino = self.post({"csrf": self.csrf(), **c})
            self.assertIn("erro=", destino)

    def test_menu_tem_conexoes_no_grupo_configurar(self):
        grupo = dict((g, i) for g, _, i in ui.MENU)["Configurar"]
        self.assertIn(("/config/conexoes", "Conexões"), grupo)

    def test_cofre_fechado_nao_derruba(self):
        with mock.patch.object(cofre, "listar", side_effect=cofre.CofreError("x")):
            self.assertIn("cofre não abriu", self.get())


class PainelHttpTest(Base):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), painel.Painel)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        config.recarregar()
        self.addCleanup(config.recarregar)
        limite.esquecer()
        self.addCleanup(limite.esquecer)
        self.cookie, self.csrf_sessao = sessao_teste.cookie_e_csrf("dono", "ana@exemplo.test")
        for p in (mock.patch.object(painel.comecar, "aberto", lambda: False),
                  mock.patch.object(painel.comecar, "ativo", lambda: False)):
            p.start()
            self.addCleanup(p.stop)

    def csrf(self):
        return self.csrf_sessao

    def req(self, caminho, campos=None, cookie=None, origem=True):
        h = {"Cookie": cookie or self.cookie}
        dados = None
        if campos is not None:
            dados = urllib.parse.urlencode(campos).encode()
            if origem:
                h["Origin"] = self.base
        r = urllib.request.Request(self.base + caminho, data=dados, headers=h)

        class Sem(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            with urllib.request.build_opener(Sem).open(r) as resp:
                return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as ex:
            return ex.code, ex.read().decode(), dict(ex.headers)

    def test_get_na_pagina_com_menu(self):
        cofre.definir("linear.token", VALOR, por="ana")
        st, corpo, _ = self.req("/config/cofre")
        self.assertEqual(st, 200)
        self.assertIn("linear.token", corpo)
        self.assertIn('href="/config/cofre" aria-current=page', corpo)
        self.assertIn("Configurar", corpo)
        self.assertNotIn(VALOR, corpo)

    def test_post_sem_csrf_403(self):
        st, _, _ = self.req("/config/cofre", {"acao": "definir", "nome": "a.b", "valor": VALOR})
        self.assertEqual(st, 403)
        st, _, _ = self.req("/config/cofre", {"acao": "definir", "nome": "a.b", "valor": VALOR, "csrf": "velho"})
        self.assertEqual(st, 403)
        self.assertEqual(cofre.listar(), [])

    def test_post_valido_usa_email_da_sessao_como_por(self):
        st, _, h = self.req("/config/cofre", {"acao": "definir", "nome": "a.b", "valor": VALOR, "csrf": self.csrf()})
        self.assertEqual(st, 303)
        self.assertTrue(h["Location"].startswith("/config/cofre?ok="))
        self.assertEqual(cofre.listar()[0]["por"], "ana@exemplo.test")
        self.assertEqual(cofre.auditoria()[0]["ip"], "127.0.0.1")

    def test_get_com_barra_final(self):
        st, corpo, _ = self.req("/config/cofre/")
        self.assertEqual(st, 200)
        self.assertIn("Novo segredo", corpo)

    def test_projetos_post_com_erro_devolve_a_pagina_200(self):
        pasta = Path(self.tmp.name) / "projetos"
        pasta.mkdir()
        with mock.patch.object(sites, "PASTA", pasta):
            campos = {"csrf": self.csrf(), "acao": "salvar", "modo": "criar", "nome": "X", "id": "../fora", "dominio": "exemplo-loja.test",
                      "contexto": "c"}
            st, corpo, _ = self.req("/config/projetos", campos)
            self.assertEqual(st, 200)
            self.assertIn("minúsculas", corpo)
            st, corpo, _ = self.req("/config/projetos")
            self.assertEqual(st, 200)
            self.assertIn('href="/config/projetos" aria-current=page', corpo)
        self.assertEqual(list(pasta.iterdir()), [])

    def test_operador_e_leitura_levam_403_no_cofre(self):
        for papel in ("operador", "leitura"):
            cookie, csrf = sessao_teste.cookie_e_csrf(papel)
            self.assertEqual(self.req("/config/cofre", cookie=cookie)[0], 403, papel)
            st, _, _ = self.req("/config/cofre", {"acao": "definir", "nome": "a.b", "valor": VALOR, "csrf": csrf}, cookie=cookie)
            self.assertEqual(st, 403, papel)
        self.assertEqual(cofre.listar(), [])

    def test_conexoes_http_get_e_post_sem_csrf(self):
        with mock.patch.object(hub, "data", lambda: Path(self.tmp.name)):
            st, corpo, _ = self.req("/config/conexoes")
            self.assertEqual(st, 200)
            self.assertIn("Conexões", corpo)
            st, _, _ = self.req("/config/conexoes", {"acao": "salvar", "conexao": "linear", "linear.token": VALOR})
            self.assertEqual(st, 403)
            st, _, h = self.req("/config/conexoes", {"acao": "salvar", "conexao": "linear", "linear.token": VALOR, "csrf": self.csrf()})
            self.assertEqual(st, 303)
        self.assertEqual(cofre.ler("linear.token"), VALOR)

    def test_post_sem_origin_403(self):
        st, _, _ = self.req("/config/cofre", {"acao": "apagar", "nome": "a.b", "csrf": self.csrf()}, origem=None)
        self.assertEqual(st, 403)


if __name__ == "__main__":
    unittest.main()
