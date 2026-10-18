import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import sites

BASE = {
    "id": "exemplo", "ids_antigos": ["exemplo-antigo"], "ativo": True, "nome": "Exemplo", "dominio": "exemplo.com.br",
    "hosts_alternativos": ["www.exemplo.com.br"], "hosts_citacao": [],
    "repo": {"github": "example-org/exemplo", "base": "main", "pacotes": "npm", "deploy": "dokploy"},
    "ids": {"gsc": "sc-domain:exemplo.com.br", "ga4": "111111112",
            "ga4_medicao": "G-X0X0XXX0X3", "clarity": "xxxxx0xxx4", "dokploy_app": None},
    "marca": {"nome": "Exemplo", "aliases": ["Ex"], "identificador": None, "nome_generico": False},
    "conselho": "CFP", "contexto": "fatos", "frase_canonica": None, "meta": None, "concorrentes": [],
    "monitor": {"buscas": ["a"], "painel": [], "locais": [2076], "max_buscas": 10, "max_prompts_painel": 14,
                "repeticoes_sem_nome": 1, "termos_gsc": ["x"]},
    "padrao": {"fora_do_google": False, "noindex_permitido": [], "treino_ia": None, "tipo_negocio": None,
               "paginas_amostra": ["/"], "indexnow": None},
    "ranking": None,
}


def com(**mudancas):
    s = copy.deepcopy(BASE)
    for caminho, valor in mudancas.items():
        alvo = s
        partes = caminho.split("__")
        for p in partes[:-1]:
            alvo = alvo[p]
        if valor is KeyError:
            del alvo[partes[-1]]
        else:
            alvo[partes[-1]] = valor
    return s


class ValidacaoTest(unittest.TestCase):
    def assertErro(self, site, trecho):
        erros = sites.validar(site, "x.json")
        self.assertTrue(any(trecho in e for e in erros), f"esperava {trecho!r} em {erros}")

    def test_base_valida(self):
        self.assertEqual(sites.validar(BASE), [])

    def test_campo_obrigatorio_e_tipo(self):
        self.assertErro(com(nome=KeyError), "falta o campo 'nome'")
        self.assertErro(com(ativo="sim"), "'ativo' deveria ser bool")
        self.assertErro(com(extra=1), "campo desconhecido 'extra'")

    def test_dominio_sem_esquema(self):
        self.assertErro(com(dominio="https://exemplo.com.br"), "'dominio' tem que ser só o host")
        self.assertErro(com(dominio="Exemplo.com.br"), "'dominio' tem que ser só o host")
        self.assertErro(com(hosts_alternativos=["exemplo.com.br/x"]), "host inválido")
        self.assertErro(com(hosts_alternativos=["exemplo.com.br"]), "repete o domínio canônico")

    def test_conselho(self):
        self.assertErro(com(conselho="CRM"), "'conselho' tem que ser")
        self.assertEqual(sites.validar(com(conselho=None)), [])

    def test_ids(self):
        self.assertErro(com(ids__ga4_medicao="UA-123"), "'ids.ga4_medicao' com formato inválido")
        self.assertErro(com(ids__gsc="exemplo.com.br"), "'ids.gsc' com formato inválido")
        self.assertErro(com(ids__gmb_cid="abc"), "'ids.gmb_cid' com formato inválido")
        self.assertEqual(sites.validar(com(ids__gmb_cid="sem_perfil")), [])
        self.assertErro(com(ids__rank_tracker="x"), "'ids.rank_tracker' desconhecido")

    def test_marca_generica_exige_alias(self):
        self.assertErro(com(marca__nome_generico=True, marca__aliases=[]), "nome genérico precisa de aliases")
        self.assertErro(com(marca__identificador="("), "não é regex válida")

    def test_monitor(self):
        self.assertErro(com(monitor__locais=[9999]), "'monitor.locais'")
        self.assertErro(com(monitor__termos_gsc=[]), "não há termo padrão")
        self.assertErro(com(monitor__buscas=[]), "'monitor.buscas' vazio")
        self.assertEqual(sites.validar(com(monitor=None)), [])

    def test_padrao(self):
        self.assertErro(com(padrao__treino_ia="talvez"), "'padrao.treino_ia'")
        self.assertErro(com(padrao__paginas_amostra=["sobre"]), "começando com '/'")
        self.assertErro(com(padrao__indexnow={"chave": "x", "aviso": None}), "indexnow.chave")
        self.assertErro(com(padrao__fora_do_google=True), "fora do Google não pode ser ativo")


class CargaTest(unittest.TestCase):
    def pasta(self, *lista):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for nome, s in lista:
            Path(tmp.name, nome).write_text(json.dumps(s), encoding="utf-8")
        return tmp.name

    def test_nome_do_arquivo_e_id(self):
        with self.assertRaisesRegex(sites.SiteInvalido, "nome do arquivo"):
            sites.carregar(self.pasta(("outro.json", BASE)))

    def test_ids_e_hosts_unicos(self):
        b = com(id="b", ids_antigos=["exemplo-antigo"], dominio="b-com-br.example", hosts_alternativos=[])
        with self.assertRaisesRegex(sites.SiteInvalido, "já usado"):
            sites.carregar(self.pasta(("exemplo.json", BASE), ("b.json", b)))
        c = com(id="c", ids_antigos=[], dominio="c-com-br.example", hosts_alternativos=["www.exemplo.com.br"])
        with self.assertRaisesRegex(sites.SiteInvalido, "já pertence"):
            sites.carregar(self.pasta(("exemplo.json", BASE), ("c.json", c)))

    def test_json_invalido_falha_alto(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        Path(tmp.name, "x.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(sites.SiteInvalido, "JSON inválido"):
            sites.carregar(tmp.name)

    def test_resumo_marca_repo_de_terceiro(self):
        terceiro = com(repo__github="jpexemplo/x")
        with tempfile.TemporaryDirectory() as dados:
            texto = sites.resumo(pasta=self.pasta(("exemplo.json", terceiro)), base_dados=dados)
            self.assertIn("Repo de terceiro", texto)
            self.assertNotIn("só leitura", texto)
            self.assertNotIn("Repo de terceiro", sites.resumo(pasta=self.pasta(("exemplo.json", BASE)), base_dados=dados))

    def test_regras_de_publicacao_e_cadencia(self):
        from hub import armazem, janelas
        hoje = dt.date.today()
        with tempfile.TemporaryDirectory() as dados:
            pasta = self.pasta(("exemplo.json", BASE))
            for d in range(5):
                janelas.registrar("exemplo", f"/p{d}", "pagina_nova", data_mudanca=(hoje - dt.timedelta(days=d)).isoformat(), base_dados=dados, pasta_sites=pasta)
            janelas.registrar("exemplo", "/velha", "pagina_nova", data_mudanca=(hoje - dt.timedelta(days=8)).isoformat(), base_dados=dados, pasta_sites=pasta)
            texto = sites.resumo(pasta=pasta, base_dados=dados)
            self.assertIn("## Regras de publicação", texto)
            self.assertIn("Nunca ranqueie a própria marca em 1º", texto)
            self.assertIn("site novo (0 cliques orgânicos em 28 dias)", texto)
            self.assertIn("já foram 5 (limite atingido", texto)
            armazem.gravar([{"dia": hoje.isoformat(), "negocio": "exemplo", "fonte": "gsc", "metrica": "cliques", "valor": 40}], base_dados=dados)
            self.assertNotIn("- Cadência:", sites.resumo(pasta=pasta, base_dados=dados))

    def test_resumo_com_repos_limita_sites_e_janelas(self):
        from hub import janelas
        outro = com(id="outro", ids_antigos=[], hosts_alternativos=[], nome="Outro", dominio="outro-com.example", repo__github="example-org/outro", ids__gsc=None)
        with tempfile.TemporaryDirectory() as dados:
            pasta = self.pasta(("exemplo.json", BASE), ("outro.json", outro))
            for site, caminho in (("exemplo", "/a"), ("exemplo", "/a"), ("outro", "/b")):  # a repetida entra uma vez só
                janelas.registrar(site, caminho, "pagina_nova", base_dados=dados, pasta_sites=pasta, pr="https://github.com/x/pull/1")
            completo = sites.resumo(pasta=pasta, base_dados=dados)
            texto = sites.resumo(pasta=pasta, base_dados=dados, repos=["example-org/EXEMPLO"])  # caixa não importa
        self.assertIn("### Exemplo (`exemplo`)", texto)
        self.assertNotIn("### Outro", texto)
        self.assertIn("- Outro (outro) · outro-com.example · example-org/outro · base main", texto)
        self.assertEqual(texto.count("- exemplo · exemplo.com.br/a · pagina_nova · julgar a partir de"), 1)
        self.assertNotIn("outro-com.example/b", texto)
        self.assertNotIn("github.com/x/pull/1", texto)  # sem repetir o PR
        self.assertIn("## Regras de publicação", texto)
        self.assertLess(len(texto), len(completo))


class RankingTest(unittest.TestCase):
    RANK = {"palavras": ["a", "b"], "local": 2076, "idioma": "pt", "dispositivo": "mobile", "profundidade": 40}

    def erros(self, **mudancas):
        return sites.validar(com(ranking={**self.RANK, **mudancas}), "x.json")

    def test_valido_e_nulo(self):
        self.assertEqual(self.erros(), [])
        self.assertEqual(sites.validar(com(ranking=None)), [])
        self.assertTrue(sites.validar(com(ranking=KeyError)))

    def test_invalidos(self):
        for mudanca in ({"palavras": []}, {"palavras": ["a", "a"]}, {"local": "2076"}, {"idioma": 1},
                        {"dispositivo": "tablet"}, {"profundidade": 5}, {"profundidade": 101}, {"extra": 1}):
            self.assertTrue(self.erros(**mudanca), mudanca)


class GravarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pasta = Path(self.tmp.name)

    def test_grava_valido_e_atomico(self):
        s = com(id="loja-teste", dominio="exemplo-loja.test", hosts_alternativos=[], ids_antigos=[])
        sites.gravar(s, self.pasta, criar=True)
        arq = self.pasta / "loja-teste.json"
        self.assertEqual(json.loads(arq.read_text(encoding="utf-8")), s)
        self.assertEqual([p.name for p in self.pasta.iterdir()], ["loja-teste.json"])  # sem tmp sobrando

    def test_invalido_nao_grava_e_erro_por_campo(self):
        s = com(id="loja-teste", dominio="Exemplo.COM/x")
        with self.assertRaises(sites.SiteInvalido) as c:
            sites.gravar(s, self.pasta, criar=True)
        self.assertIn("dominio", c.exception.campos)
        self.assertEqual(list(self.pasta.iterdir()), [])

    def test_id_malicioso_recusado(self):
        for ruim in ("../fora", "a/b", "..", "A", "a b", ""):
            with self.assertRaises(sites.SiteInvalido, msg=ruim):
                sites.gravar(com(id=ruim), self.pasta, criar=True)
        self.assertEqual(list(self.pasta.iterdir()), [])
        self.assertFalse((self.pasta.parent / "fora.json").exists())

    def test_id_repetido_ao_criar(self):
        s = com(id="loja-teste", dominio="exemplo-loja.test", hosts_alternativos=[], ids_antigos=[])
        sites.gravar(s, self.pasta, criar=True)
        with self.assertRaises(sites.SiteInvalido) as c:
            sites.gravar(s, self.pasta, criar=True)
        self.assertIn("id", c.exception.campos)
        sites.gravar(com(id="loja-teste", nome="Outro", dominio="exemplo-loja.test", hosts_alternativos=[], ids_antigos=[]), self.pasta)  # editar pode

    def test_criacao_exclusiva_contra_corrida(self):
        s = com(id="loja-teste", dominio="exemplo-loja.test", hosts_alternativos=[], ids_antigos=[])
        original = sites.Path.exists
        with mock.patch.object(sites.Path, "exists", lambda self: False if self.name == "loja-teste.json" else original(self)):
            sites.gravar(s, self.pasta, criar=True)  # outro processo ainda não tinha gravado: passa a checagem de exists
            with self.assertRaises(sites.SiteInvalido) as c:
                sites.gravar({**s, "nome": "Corrida"}, self.pasta, criar=True)  # mas o link exclusivo recusa
        self.assertIn("id", c.exception.campos)
        self.assertEqual(json.loads((self.pasta / "loja-teste.json").read_text(encoding="utf-8"))["nome"], s["nome"])
        self.assertEqual([p.name for p in self.pasta.iterdir()], ["loja-teste.json"])

    def test_tmp_unico_e_limpo_se_falha(self):
        s = com(id="loja-teste", dominio="exemplo-loja.test", hosts_alternativos=[], ids_antigos=[])
        with mock.patch.object(sites.os, "replace", side_effect=OSError("disco")):
            with self.assertRaises(OSError):
                sites.gravar(s, self.pasta)
        self.assertEqual(list(self.pasta.iterdir()), [])

    def test_contexto_vazio_em_portugues(self):
        with self.assertRaises(sites.SiteInvalido) as c:
            sites.gravar(com(id="loja-teste", contexto="  "), self.pasta, criar=True)
        self.assertIn("vazio", c.exception.campos["contexto"])
        self.assertNotIn("deveria ser", c.exception.campos["contexto"])

    def test_host_de_outro_site_recusado(self):
        sites.gravar(com(id="a-loja", dominio="exemplo-loja.test", hosts_alternativos=[], ids_antigos=[]), self.pasta, criar=True)
        with self.assertRaises(sites.SiteInvalido) as c:
            sites.gravar(com(id="b-loja", dominio="exemplo-loja.test", hosts_alternativos=[], ids_antigos=[]), self.pasta, criar=True)
        self.assertIn("dominio", c.exception.campos)


if __name__ == "__main__":
    unittest.main()
