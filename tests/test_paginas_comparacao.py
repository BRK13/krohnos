import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path

from hub import paginas_comparacao as pc

HOJE = dt.date(2026, 10, 21)

CSV = """Top queries,Clicks,Impressions,Position,Page
alternativas ao concorrente a,0,120,12.0,
marca-exemplo ou concorrente a,0,60,9.5,
concorrente a,1,300,6.0,
tendinite ou tendinose,0,200,14.2,
melhor clínica em bairro alfa,0,90,11.0,
melhor clínica em bairro beta,0,80,10.0,
dor no joelho ao correr,2,150,9.0,https://marca-exemplo.test/dor-no-joelho
kinesio tape funciona,0,70,13.0,
quanto custa uma consulta,0,40,15.0,
receita de bolo,0,500,3.0,
tendinite rara,0,5,20.0,
"""

BOM = """---
molde: vs
termo: marca-exemplo ou concorrente a
marca: Marca Exemplo
concorrente: Concorrente A
lugar:
atendimento: n/a
conselho: none
verificado_em: 2026-10-15
---

# Marca Exemplo ou Concorrente A: qual serve para você?

## Resposta direta
Os dois atendem adultos. A Marca Exemplo publica horário até as 20h e o Concorrente A publica horário até as 19h.

## Critério
Critério: o que cada site publica em 15/10/2026, em ordem alfabética.

## Fatos
- A Marca Exemplo atende até as 20h. [fonte: https://marca-exemplo.test/horarios]
- O Concorrente A lista 40 convênios. [fonte: https://concorrente-a.test/convenios]

## Tabela
| Critério | Marca | Outro lado | Fonte |
| --- | --- | --- | --- |
| Horário | até 20h | até 19h | https://concorrente-a.test/horarios |

## Onde o concorrente ganha
- Lista mais convênios. [fonte: https://concorrente-a.test/convenios]

## Perguntas frequentes
- Qual atende aos sábados? / O Concorrente A não informa sábado no site consultado.
"""


def com(texto, antigo, novo):
    assert antigo in texto, antigo
    return texto.replace(antigo, novo, 1)


def erros_de(texto, **kw):
    return pc.portao(texto, hoje=HOJE, **kw)[0]


class ClassificarTest(unittest.TestCase):
    def test_moldes(self):
        c = ["Concorrente A"]
        self.assertEqual(pc.classificar("alternativas ao Concorrente A", c)[:2], ("alternativas", "Concorrente A"))
        self.assertEqual(pc.classificar("Concorrente A preço", c)[:2], ("nominal", "Concorrente A"))
        self.assertEqual(pc.classificar("tendinite ou tendinose")[0], "vs")
        self.assertEqual(pc.classificar("kinesio tape funciona")[0], "funciona")
        self.assertEqual(pc.classificar("dor no joelho ao correr")[0], "dor-servico")
        self.assertEqual(pc.classificar("quanto custa uma consulta")[0], "pergunta")
        self.assertEqual(pc.classificar("clínica perto de mim")[0], "melhor-em-local")
        self.assertEqual(pc.classificar("alternatives to Concorrente A", c)[0], "alternativas")
        self.assertIsNone(pc.classificar("receita de bolo")[0])

    def test_nome_de_concorrente_so_com_borda(self):
        self.assertIsNone(pc.classificar("xconcorrente ay", ["Concorrente A"])[1])


class TermosTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = Path(self.tmp.name) / "gsc.csv"
        self.csv.write_text(CSV, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def itens(self, **kw):
        args = dict(concorrentes=["Concorrente A"], lugares=["bairro alfa", "bairro beta"], atendemos=["bairro alfa"], paginas=[])
        args.update(kw)
        return {i["termo"]: i for i in pc.listar_termos(pc.ler_gsc(self.csv), **args)}

    def test_cabecalho_em_portugues(self):
        p = Path(self.tmp.name) / "pt.csv"
        p.write_text("Consultas principais,Cliques,Impressões,Posição\nfoo bar,1,100,\"8,5\"\n", encoding="utf-8")
        r = pc.ler_gsc(p)[0]
        self.assertEqual((r["impressions"], r["position"]), (100, 8.5))

    def test_lista_priorizada_e_filtros(self):
        t = self.itens()
        self.assertNotIn("receita de bolo", t)  # não serve a nenhum molde
        self.assertNotIn("tendinite rara", t)  # abaixo do mínimo de impressões
        self.assertEqual(t["alternativas ao concorrente a"]["molde"], "alternativas")
        self.assertIn("fora do ar até revisão", t["alternativas ao concorrente a"]["motivo"])
        self.assertEqual(t["concorrente a"]["molde"], "nominal")
        ordem = [i["acao"] for i in pc.listar_termos(pc.ler_gsc(self.csv), ["Concorrente A"], ["bairro alfa", "bairro beta"], ["bairro alfa"])]
        self.assertEqual(ordem, sorted(ordem, key={"criar": 0, "reforcar": 1, "descartar": 2}.get))

    def test_lugar_sem_atendimento_e_descartado(self):
        t = self.itens()
        self.assertEqual(t["melhor clínica em bairro beta"]["acao"], "descartar")
        self.assertEqual(t["melhor clínica em bairro alfa"]["acao"], "criar")

    def test_pagina_existente_vira_reforcar(self):
        t = self.itens()
        self.assertEqual(t["dor no joelho ao correr"]["acao"], "reforcar")  # a coluna page já cobre
        t = self.itens(paginas=[("https://marca-exemplo.test/kinesio-tape", "Kinesio tape: funciona?")])
        self.assertEqual(t["kinesio tape funciona"]["acao"], "reforcar")


class RascunhosTest(unittest.TestCase):
    def test_esqueleto_nao_escreve_fato_e_nao_passa_no_portao(self):
        item = {"termo": "tendinite ou tendinose", "molde": "vs", "concorrente": None, "lugar": None}
        for idioma in ("pt", "en"):
            texto = pc.esqueleto(item, "Marca Exemplo", idioma)
            self.assertIn("tendinite ou tendinose", texto)
            erros = erros_de(texto, idioma=idioma)
            self.assertTrue(any("PREENCHER" in e or "FILL" in e for e in erros), erros)

    def test_cli_gera_so_os_criar(self):
        with tempfile.TemporaryDirectory() as d:
            termos = Path(d) / "t.json"
            termos.write_text(json.dumps([
                {"termo": "kinesio tape funciona", "molde": "funciona", "acao": "criar"},
                {"termo": "dor no joelho", "molde": "dor-servico", "acao": "reforcar"}]), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pc.main(["rascunhos", str(termos), "--pasta", str(Path(d) / "r"), "--marca", "Marca Exemplo"]), 0)
            self.assertEqual([p.name for p in (Path(d) / "r").iterdir()], ["kinesio-tape-funciona.md"])


class PortaoTest(unittest.TestCase):
    def test_rascunho_bom_passa(self):
        self.assertEqual(pc.portao(BOM, hoje=HOJE), ([], []))
        self.assertEqual(pc.portao(BOM, "en", hoje=HOJE)[0], [])

    def test_campo_obrigatorio_e_molde(self):
        self.assertTrue(any("marca" in e for e in erros_de(com(BOM, "marca: Marca Exemplo\n", ""))))
        self.assertTrue(any("molde desconhecido" in e for e in erros_de(com(BOM, "molde: vs", "molde: x"))))
        self.assertTrue(any("concorrente" in e for e in erros_de(com(com(BOM, "molde: vs", "molde: nominal"), "concorrente: Concorrente A", "concorrente:"))))

    def test_fato_sem_fonte_e_linha_sem_url(self):
        e = erros_de(com(BOM, " [fonte: https://marca-exemplo.test/horarios]", ""))
        self.assertTrue(any("fato sem fonte" in x for x in e), e)
        e = erros_de(com(BOM, "| Horário | até 20h | até 19h | https://concorrente-a.test/horarios |", "| Horário | até 20h | até 19h | site |"))
        self.assertTrue(any("sem URL" in x for x in e), e)

    def test_promessa_de_resultado(self):
        for frase in ("Resultado garantido em 30 dias.", "100% de sucesso.", "Nós curamos a dor.", "Satisfaction guaranteed."):
            e = erros_de(com(BOM, "Os dois atendem adultos.", frase))
            self.assertTrue(any("promessa" in x for x in e), (frase, e))

    def test_autorranking(self):
        e = erros_de(com(BOM, "Os dois atendem adultos.", "A Marca Exemplo é a melhor da cidade."))
        self.assertTrue(any("superlativo" in x for x in e), e)
        lista = com(BOM, "molde: vs", "molde: alternativas") + "\n1. Marca Exemplo\n2. Concorrente A\n"
        self.assertTrue(any("1º" in x for x in erros_de(lista)))
        self.assertFalse(any("1º" in x for x in erros_de(com(lista, "1. Marca Exemplo\n2. Concorrente A", "1. Concorrente A\n2. Marca Exemplo"))))
        self.assertTrue(any("Critério:" in x for x in erros_de(com(lista, "Critério: o que", "Nota: o que"))))

    def test_superlativo_do_concorrente_nao_conta_como_autorranking(self):
        self.assertFalse(any("superlativo" in x for x in erros_de(com(BOM, "Os dois atendem adultos.", "O Concorrente A diz ser o líder."))))

    def test_lugar_sem_atendimento(self):
        local = com(com(BOM, "lugar:", "lugar: Bairro Alfa"), "atendimento: n/a", "atendimento: nao")
        self.assertTrue(any("sem atendimento" in x for x in erros_de(local)))
        self.assertFalse(any("atendimento" in x for x in erros_de(com(local, "atendimento: nao", "atendimento: sim"))))
        self.assertTrue(any("lugar" in x for x in erros_de(com(BOM, "molde: vs", "molde: melhor-em-local"))))

    def test_linguagem_que_denigre_e_nao_tem(self):
        self.assertTrue(any("denigre" in x for x in erros_de(com(BOM, "Os dois atendem adultos.", "O Concorrente A é um golpe."))))
        e = erros_de(com(BOM, "Os dois atendem adultos.", "O Concorrente A não tem sábado."))
        self.assertTrue(any("não informa" in x for x in e), e)
        self.assertFalse(any("não informa" in x for x in erros_de(BOM)))  # "não informa" é o jeito certo

    def test_concorrente_precisa_ganhar_em_algo(self):
        e = erros_de(com(BOM, "- Lista mais convênios. [fonte: https://concorrente-a.test/convenios]", ""))
        self.assertTrue(any("Onde o concorrente ganha" in x for x in e), e)

    def test_conselho(self):
        cfp = com(BOM, "conselho: none", "conselho: CFP")
        self.assertTrue(any("antes e depois" in x for x in erros_de(com(cfp, "Os dois atendem adultos.", "Veja o antes e depois de pacientes."))))
        self.assertTrue(any("primeira pessoa" in x for x in erros_de(com(cfp, "Os dois atendem adultos.", "Vejo muito isso, minhas pacientes relatam."))))
        self.assertFalse(any("primeira pessoa" in x for x in erros_de(com(BOM, "Os dois atendem adultos.", "Vejo muito isso."))))  # sem conselho: não bloqueia
        oab = com(BOM, "conselho: none", "conselho: OAB")
        self.assertTrue(any("honorários" in x for x in erros_de(com(oab, "Os dois atendem adultos.", "Honorários a partir de R$ 500."))))
        self.assertTrue(any("conselho desconhecido" in x for x in erros_de(com(BOM, "conselho: none", "conselho: XYZ"))))

    def test_data(self):
        self.assertTrue(any("futuro" in x for x in erros_de(com(BOM, "2026-10-15", "2026-12-01"))))
        self.assertTrue(any("inválido" in x for x in erros_de(com(BOM, "2026-10-15", "15/10/2026"))))
        avisos = pc.portao(com(BOM, "2026-10-15", "2026-01-01"), hoje=HOJE)[1]
        self.assertTrue(any("mais de 90 dias" in x for x in avisos))

    def test_resposta_longa_e_so_aviso(self):
        longa = com(BOM, "Os dois atendem adultos.", "palavra " * 100)
        erros, avisos = pc.portao(longa, hoje=HOJE)
        self.assertEqual(erros, [])
        self.assertTrue(any("palavras" in x for x in avisos))

    def test_irmas_parecidas_demais(self):
        outra = com(BOM, "Horário | até 20h", "Horário | até 20h")  # igual
        achados = pc.irmas({"a.md": BOM, "b.md": outra})
        self.assertEqual(len(achados), 1)
        diferente = "---\nmolde: pergunta\n---\n# Outra coisa\n## Resposta direta\nTexto totalmente diferente sobre outro assunto sem relação alguma com horários."
        self.assertEqual(pc.irmas({"a.md": BOM, "c.md": diferente}), [])

    def test_cli_codigo_de_saida(self):
        with tempfile.TemporaryDirectory() as d:
            ok, ruim = Path(d) / "ok.md", Path(d) / "ruim.md"
            ok.write_text(BOM.replace("2026-10-15", dt.date.today().isoformat()), encoding="utf-8")
            ruim.write_text(com(BOM, "Os dois atendem adultos.", "Resultado garantido."), encoding="utf-8")
            saida = io.StringIO()
            with contextlib.redirect_stdout(saida):
                self.assertEqual(pc.main(["portao", str(ok)]), 0)
                self.assertEqual(pc.main(["portao", str(ruim)]), 1)
            self.assertIn("REPROVADO", saida.getvalue())


if __name__ == "__main__":
    unittest.main()
