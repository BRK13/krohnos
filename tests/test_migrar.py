import json
import tempfile
import unittest
from pathlib import Path

from hub import migrar, sites

MAPA = {"clienteipsi-v2": "clientei", "cliente-a": "fsite05", "clienteh": "clientej"}


def escrever(caminho, texto="x"):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(texto, encoding="utf-8")


class MigrarTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = Path(tmp.name, "reports")
        self.home = Path(tmp.name, "so")
        escrever(self.dados / "aio/clienteipsi-v2/terapia/2026-10-06.json", "a" * 10)
        escrever(self.dados / "aio/clienteipsi-v2/painel/2026-W41.jsonl", "b")
        escrever(self.dados / "geo-new/cliente-a/latest.json", "c")
        escrever(self.dados / "geo-new/psite13/latest.json", "não muda")
        escrever(self.dados / "snapshots/2026-10-07/cliente-a-status.json", "d")
        escrever(self.dados / "snapshots/2026-10-07/fsite06-status.json", "não muda")
        escrever(self.dados / "snapshots/2026-10-07/clienteh-gsc.json", "e")
        escrever(self.home / "credenciais/clarity/clienteh.json", '{"token": "t"}')

    def test_mapa_real_vem_do_cadastro(self):
        m = sites.mapa_ids_antigos()
        self.assertEqual(m["clienteipsi-v2"], "clientei")
        self.assertEqual(m["cliente-a"], "fsite05")
        self.assertEqual(m["cliente-c"], "asite01")
        self.assertNotIn("psite13", m)  # id igual: nada a mover
        self.assertNotIn("fsite06", m)

    def test_plano_nao_muda_nada(self):
        r = migrar.migrar(MAPA, self.dados, self.home)
        self.assertFalse(r["aplicado"])
        self.assertEqual(len(r["plano"]), 5)
        self.assertTrue((self.dados / "aio/clienteipsi-v2").exists())

    def test_aplica_confere_e_e_idempotente(self):
        r = migrar.migrar(MAPA, self.dados, self.home, executar=True)
        self.assertEqual(len(r["movimentos"]), 5)
        self.assertEqual((self.dados / "aio/clientei/terapia/2026-10-06.json").read_text(encoding="utf-8"), "a" * 10)
        self.assertTrue((self.dados / "aio/clientei/painel/2026-W41.jsonl").exists())
        self.assertTrue((self.dados / "geo-new/fsite05/latest.json").exists())
        self.assertTrue((self.dados / "snapshots/2026-10-07/fsite05-status.json").exists())
        self.assertTrue((self.dados / "snapshots/2026-10-07/fsite06-status.json").exists())  # prefixo parecido, intocado
        self.assertTrue((self.dados / "snapshots/2026-10-07/clientej-gsc.json").exists())
        self.assertEqual(json.loads((self.home / "credenciais/clarity/clientej.json").read_text()), {"token": "t"})
        self.assertFalse((self.dados / "aio/clienteipsi-v2").exists())
        self.assertEqual(len(list((self.dados / "migracoes").glob("*-ids.json"))), 1)
        r2 = migrar.migrar(MAPA, self.dados, self.home, executar=True)
        self.assertEqual(r2["movimentos"], [])

    def test_junta_pasta_existente_sem_colisao(self):
        escrever(self.dados / "aio/clientei/nova-busca/2026-10-08.json")
        migrar.migrar(MAPA, self.dados, self.home, executar=True)
        self.assertTrue((self.dados / "aio/clientei/nova-busca/2026-10-08.json").exists())
        self.assertTrue((self.dados / "aio/clientei/terapia/2026-10-06.json").exists())
        self.assertFalse((self.dados / "aio/clienteipsi-v2").exists())

    def test_conflito_aborta_sem_mexer_em_nada(self):
        escrever(self.dados / "snapshots/2026-10-07/fsite05-status.json", "já existe")
        with self.assertRaisesRegex(RuntimeError, "conflito"):
            migrar.migrar(MAPA, self.dados, self.home, executar=True)
        self.assertTrue((self.dados / "aio/clienteipsi-v2/terapia/2026-10-06.json").exists())
        self.assertFalse((self.dados / "aio/clientei").exists())


if __name__ == "__main__":
    unittest.main()
