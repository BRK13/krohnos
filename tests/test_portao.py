"""portão de Done para issue de dado (coleta, carga, classificação, leitura por IA)."""
import json
import tempfile
import unittest
from pathlib import Path

from hub import portao

PROVA = """Feito.

## Prova de execução
- Antes: 0 teses lidas. Depois: 1.482 teses lidas (select count(*) from tese).
- Agendamento: cron instalado em deploy/nimbus.cron, diário 03:00.
"""


def item(titulo="Classificar os óbices do STJ", descricao="Rodar a classificação e importar os dados"):
    return {"identifier": "KRN-1", "titulo": titulo, "descricao": descricao}


class Detecta(unittest.TestCase):
    def test_pelo_texto_da_issue(self):
        self.assertTrue(portao.eh_de_dado(item(), []))
        self.assertTrue(portao.eh_de_dado(item("Coletar acórdãos do TJMG", ""), []))

    def test_pelos_arquivos_do_pr(self):
        self.assertTrue(portao.eh_de_dado(item("Ajuste", "texto neutro"), ["coleta/atualizar_stj.py"]))
        self.assertTrue(portao.eh_de_dado(item("Ajuste", "texto neutro"), ["deploy/nimbus.cron"]))

    def test_issue_comum_nao_e_de_dado(self):
        self.assertFalse(portao.eh_de_dado(item("Trocar a cor do botão", "Mudar o CSS"), ["src/app/page.tsx"]))


class Prova(unittest.TestCase):
    def test_bloco_completo_passa(self):
        self.assertEqual(portao.faltou_prova(PROVA), "")

    def test_sem_bloco(self):
        self.assertIn("Prova de execução", portao.faltou_prova("Feito, PR aberto."))

    def test_sem_contagem_antes_depois(self):
        txt = "## Prova de execução\n- Agendamento: cron instalado, diário."
        self.assertIn("antes/depois", portao.faltou_prova(txt))

    def test_sem_agendamento(self):
        txt = "## Prova de execução\n- Antes: 0. Depois: 10 linhas."
        self.assertIn("agendamento", portao.faltou_prova(txt))


class Avaliar(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.criadas = []

    def av(self, texto, it=None):
        return portao.avaliar(it or item(), texto, arquivos_fn=lambda t: [],
                              criar_fn=lambda *a, **k: self.criadas.append(a) or "KRN-2 u", base=self.base)

    def test_issue_de_dado_sem_prova_volta(self):
        r = self.av("PR aberto.")
        self.assertFalse(r["ok"])
        self.assertIn("Prova de execução", r["motivo"])

    def test_com_prova_passa(self):
        self.assertTrue(self.av(PROVA)["ok"])

    def test_issue_comum_passa_sem_prova(self):
        self.assertTrue(self.av("feito", item("Trocar a cor", "CSS"))["ok"])

    def test_nao_roda_em_massa_abre_issue_de_execucao(self):
        r = self.av("Código pronto. Não roda em massa nesta issue: a carga fica para a execução.")
        self.assertTrue(r["ok"])
        self.assertEqual(r["execucao"], "KRN-2 u")
        self.assertEqual(len(self.criadas), 1)
        self.assertEqual(self.av("Não roda em massa aqui.")["execucao"], "")  # não duplica
        self.assertEqual(len(self.criadas), 1)

    def test_registra_pendencia_e_lista_no_resumo(self):
        self.av("PR aberto.")
        linhas = portao.parado_ou_so_codigo(self.base)
        self.assertTrue(any("KRN-1" in x for x in linhas))
        self.av(PROVA)  # provou depois: sai da lista
        self.assertFalse(any("KRN-1" in x for x in portao.parado_ou_so_codigo(self.base)))


class Resumo(unittest.TestCase):
    def test_bloco_parado_ou_so_codigo_no_resumo(self):
        import datetime as dt
        from hub import resumo
        with tempfile.TemporaryDirectory() as d:
            portao.avaliar(item(), "PR aberto.", arquivos_fn=lambda t: [], base=d)
            out = "\n".join(resumo.parado_ou_so_codigo(Path(d), dt.datetime.now(dt.timezone.utc)))
        self.assertIn("Parado ou só código", out)
        self.assertIn("KRN-1", out)


if __name__ == "__main__":
    unittest.main()
