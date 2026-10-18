import unittest

from hub import citacao, sites

FSITE05 = {"nome": "Cliente A", "nomes": ["Cliente A", "ClienteA"], "identificador": None,
         "hosts": {"clientea-com.example", "www.clientea-com.example"}}


class MencaoTest(unittest.TestCase):
    def test_palavra_inteira_sem_acento_e_sem_dupla_contagem(self):
        self.assertEqual(citacao.mencoes("Cliente A, CLIENTE A e áfsite05", ["fsite05", "Cliente A"])[0], 2)
        self.assertEqual(citacao.mencoes("Método ClienteM", ["metodo clientem"])[0], 1)
        self.assertEqual(citacao.mencoes("clienteaissimo", ["ClienteA"])[0], 0)

    def test_posicao_da_primeira(self):
        self.assertEqual(citacao.mencoes("a Zenklub e a Vittude", ["Vittude"]), (1, 14))
        self.assertEqual(citacao.mencoes("nada", []), (0, None))

    def test_marca_de_nome_generico_so_pelos_aliases(self):
        a = citacao.alvo(sites.site("clientej"))
        self.assertNotIn("ClienteJ", a["nomes"])
        r = citacao.medir_resposta("Pesquise clientej do STJ no jsite10.", [], a, ["jsite10"])
        self.assertFalse(r["marca_mencionada"])
        r = citacao.medir_resposta("Use o clientej-com-br.example.", [], a, [])
        self.assertTrue(r["marca_mencionada"])


class CitacaoTest(unittest.TestCase):
    def test_host_exato_ou_declarado(self):
        urls = ["https://evilclientea-com.example/a", "https://clientea-com.example.evil.com/b", "https://blog.clientea-com.example/c"]
        self.assertFalse(citacao.citado(urls, FSITE05["hosts"]))  # subdomínio não declarado não conta
        self.assertTrue(citacao.citado(["https://www.clientea-com.example/"], FSITE05["hosts"]))
        self.assertTrue(citacao.citado(["https://clientea-com.example./x"], FSITE05["hosts"]))
        self.assertTrue(citacao.citado(["https://blog.clientea-com.example/c"], FSITE05["hosts"] | {"blog.clientea-com.example"}))

    def test_hosts_vem_do_cadastro(self):
        a = citacao.alvo(sites.site("asite01"))
        self.assertEqual(a["hosts"], {"clientec-com.example", "www.clientec-com.example", "app.clientec-com.example"})
        self.assertTrue(citacao.citado(["https://app.clientec-com.example/login"], a["hosts"]))

    def test_url_lixo_nao_quebra(self):
        self.assertEqual(citacao.host("http://[::1"), "")


class MedirRespostaTest(unittest.TestCase):
    def test_sem_nome_no_prompt(self):
        texto = "Opções: Fisioclin, depois a Cliente A (https://clientea-com.example/convenios)."
        r = citacao.medir_resposta(texto, [], FSITE05, ["Fisioclin", "Posture"], "Qual a melhor clínica?")
        self.assertFalse(r["com_marca"])
        self.assertTrue(r["marca_mencionada"])
        self.assertTrue(r["dominio_citado"])
        self.assertEqual(r["ordem"], ["Fisioclin", "Cliente A"])
        self.assertEqual(r["primeiro"], "Fisioclin")
        self.assertEqual(r["concorrentes"], ["Fisioclin"])

    def test_com_nome_no_prompt_e_reconhecimento(self):
        fern = citacao.alvo(sites.site("clientei"))
        r = citacao.medir_resposta("A Cliente F tem CRP 01/12345.", [], fern, [], "Quem é a ClienteI Pessoa Teixeira?")
        self.assertTrue(r["com_marca"])
        self.assertTrue(r["reconhece"])
        r = citacao.medir_resposta("Não encontrei informações sobre ela.", [], fern, [], "Quem é a ClienteI Pessoa Teixeira?")
        self.assertFalse(r["reconhece"])

    def test_alias_nao_vem_de_posicao_de_palavra(self):
        # O código antigo montava "Cliente F" pegando palavras alternadas do nome. Agora só alias declarado.
        a = {"nome": "Ana Maria Souza", "nomes": ["Ana Maria Souza"], "identificador": None, "hosts": {"ana-com.example"}}
        self.assertFalse(citacao.medir_resposta("Ana Souza atende online.", [], a, [])["marca_mencionada"])


if __name__ == "__main__":
    unittest.main()
