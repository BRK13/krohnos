import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import verificar_fontes as vf

SITE = {"id": "loja-teste", "dominio": "exemplo-loja.test",
        "repo": {"github": "dono/exemplo-loja", "base": "main", "pacotes": "yarn", "deploy": "dokploy"},
        "ids": {"gsc": "sc-domain:exemplo-loja.test", "ga4": "123456789", "ga4_medicao": "G-ABCDEF1234",
                "clarity": "abcdef1234", "gmb_cid": "12345678901"}}
TOKEN = "eyJsegredo.do.clarity-9999"  # gitleaks:allow (valor falso de teste)


class Falso:
    """Cliente falso: guarda as chamadas, devolve o programado, nada de rede."""
    def __init__(self, resposta=None, erro=None):
        self.resposta, self.erro, self.chamadas = resposta, erro, []

    def __call__(self, *a, **k):
        self.chamadas.append((a, k))
        if self.erro:
            raise self.erro
        return self.resposta


class FontesTest(unittest.TestCase):
    def test_gsc_ok_e_fora(self):
        st, msg = vf.gsc(SITE, Falso([{"siteUrl": "sc-domain:exemplo-loja.test"}]))
        self.assertEqual(st, "ok")
        st, msg = vf.gsc(SITE, Falso([{"siteUrl": "sc-domain:outro.test"}]))
        self.assertEqual(st, "erro")
        self.assertIn("Search Console", msg)

    def test_gsc_sem_id_e_excecao(self):
        self.assertEqual(vf.gsc({**SITE, "ids": {}}, Falso([]))[0], "nao_configurado")
        self.assertEqual(vf.gsc({**SITE, "ids": {"gsc": None}}, Falso([]))[0], "nao_configurado")
        st, msg = vf.gsc(SITE, Falso(erro=OSError("rede caiu")))
        self.assertEqual(st, "erro")
        self.assertIn("OSError", msg)

    def test_ga4(self):
        f = Falso({"rows": []})
        self.assertEqual(vf.ga4(SITE, f)[0], "ok")
        self.assertEqual(f.chamadas[0][0][0], "123456789")
        self.assertEqual(vf.ga4({**SITE, "ids": {}}, Falso({}))[0], "nao_configurado")
        self.assertEqual(vf.ga4(SITE, Falso(erro=RuntimeError("403")))[0], "erro")

    def test_clarity_usa_token_do_projeto_sem_vazar(self):
        f = Falso((200, "{}"))
        with mock.patch.object(vf, "_token_clarity", lambda sid: TOKEN):
            st, msg = vf.clarity(SITE, f)
        self.assertEqual(st, "ok")
        self.assertEqual(f.chamadas[0][1]["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, msg)
        with mock.patch.object(vf, "_token_clarity", lambda sid: TOKEN):
            st, msg = vf.clarity(SITE, Falso((401, "")))
        self.assertEqual(st, "erro")
        self.assertNotIn(TOKEN, msg)
        with mock.patch.object(vf, "_token_clarity", lambda sid: None):
            st, msg = vf.clarity(SITE, Falso((200, "")))
        self.assertEqual(st, "erro")
        self.assertIn("token", msg)

    def test_clarity_429_e_cota_do_dia_nao_erro(self):
        with mock.patch.object(vf, "_token_clarity", lambda sid: TOKEN):
            st, msg = vf.clarity(SITE, Falso((429, "")))
        self.assertEqual(st, "ok")
        self.assertIn("cota", msg)

    def test_gmb_nunca_verde_sem_prova(self):
        f = Falso((200, ""))
        st, msg = vf.gmb(SITE, f)
        self.assertEqual(st, "manual")
        self.assertIn("confirme no Perfil da Empresa", msg)
        self.assertEqual(f.chamadas, [])  # nenhuma chamada: nada a provar daqui
        self.assertEqual(vf.gmb({**SITE, "ids": {"gmb_cid": "sem_perfil"}})[0], "manual")
        self.assertEqual(vf.gmb({**SITE, "ids": {"gmb_cid": "abc"}})[0], "erro")
        self.assertEqual(vf.gmb({**SITE, "ids": {}})[0], "nao_configurado")

    def test_pagespeed(self):
        j = {"lighthouseResult": {"categories": {"performance": {"score": 0.91}}, "audits": {}}}
        f = Falso(j)
        st, msg = vf.pagespeed(SITE, f)
        self.assertEqual(st, "ok")
        self.assertIn("91", msg)
        self.assertIn("https%3A%2F%2Fexemplo-loja.test", f.chamadas[0][0][0])
        self.assertEqual(vf.pagespeed(SITE, Falso({}))[0], "erro")
        self.assertEqual(vf.pagespeed(SITE, Falso(erro=OSError("x")))[0], "erro")

    def test_repo(self):
        f = Falso((0, '{"full_name": "dono/exemplo-loja"}'))
        st, _ = vf.repo(SITE, f)
        self.assertEqual(st, "ok")
        self.assertEqual(f.chamadas[0][0][0], ["api", "repos/dono/exemplo-loja"])
        st, msg = vf.repo(SITE, Falso((1, "Not Found")))
        self.assertEqual(st, "erro")
        self.assertIn("exemplo-loja", msg)
        self.assertEqual(vf.repo({**SITE, "repo": None}, Falso((0, "")))[0], "nao_configurado")

    def test_deploy(self):
        f = Falso((200, "<html>"))
        self.assertEqual(vf.deploy(SITE, f)[0], "ok")
        self.assertEqual(f.chamadas[0][0][0], "https://exemplo-loja.test/")
        st, msg = vf.deploy(SITE, Falso((502, "")))
        self.assertEqual(st, "erro")
        self.assertIn("502", msg)


class GuardaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(vf, "_pasta", lambda: Path(self.tmp.name) / "verificacoes")
        p.start()
        self.addCleanup(p.stop)

    def test_verificar_grava_status_e_mensagem(self):
        st, msg = vf.verificar(SITE, "deploy", cliente=Falso((200, "")), agora="2026-10-17T10:00:00+00:00")
        self.assertEqual(st, "ok")
        d = json.loads((Path(self.tmp.name) / "verificacoes" / "loja-teste.json").read_text(encoding="utf-8"))
        self.assertEqual(d["deploy"], {"status": "ok", "mensagem": msg, "em": "2026-10-17T10:00:00+00:00",
                           "id_verificado": "exemplo-loja.test"})
        vf.verificar(SITE, "repo", cliente=Falso((1, "x")), agora="2026-10-17T10:01:00+00:00")
        d = vf.ler("loja-teste")
        self.assertEqual(d["deploy"]["status"], "ok")  # não apaga as outras fontes
        self.assertEqual(d["repo"]["status"], "erro")

    def test_fonte_desconhecida(self):
        with self.assertRaises(ValueError):
            vf.verificar(SITE, "orkut", cliente=Falso())

    def test_arquivo_corrompido_nao_derruba(self):
        pasta = Path(self.tmp.name) / "verificacoes"
        pasta.mkdir()
        (pasta / "loja-teste.json").write_text("{quebrado", encoding="utf-8")
        self.assertEqual(vf.ler("loja-teste"), {})
        (pasta / "loja-teste.json").write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(vf.ler("loja-teste"), {})
        st, _ = vf.verificar(SITE, "deploy", cliente=Falso((200, "")))  # e a próxima gravação conserta
        self.assertEqual(vf.ler("loja-teste")["deploy"]["status"], "ok")

    def test_id_malicioso_nao_vira_caminho(self):
        self.assertEqual(vf.ler("../../etc/passwd"), {})
        with self.assertRaises(ValueError):
            vf.verificar({**SITE, "id": "../x"}, "deploy", cliente=Falso((200, "")))

    def test_excecao_na_fonte_vira_erro_gravado(self):
        st, _ = vf.verificar(SITE, "deploy", cliente=Falso(erro=OSError("boom")))
        self.assertEqual(st, "erro")
        self.assertEqual(vf.ler("loja-teste")["deploy"]["status"], "erro")


if __name__ == "__main__":
    unittest.main()
