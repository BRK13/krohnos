import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path

from hub import segredos_fonte as sf

V1, V2 = "sk-or-valor-um-0123456789", "sk-or-valor-dois-9876543210"


class Fonte(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.dest = self.t / "segredos"
        (self.t / "a.key").write_text(V1 + "\n")
        (self.t / "b.env").write_text(f"OUTRA=x\nexport CHAVE='{V1}'\n")
        (self.t / "c.env").write_text(f"CHAVE={V2}\n")
        self.fontes = {"CHAVE": [(self.t / "a.key", None), (self.t / "b.env", "CHAVE")]}

    def rodar(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sf.main(argv, fontes=self.fontes, destino=self.dest, grupo=None)
        return rc, out.getvalue() + err.getvalue()

    def test_dry_run_nao_escreve(self):
        rc, saida = self.rodar(["migrar"])
        self.assertEqual(rc, 0)
        self.assertFalse(self.dest.exists())
        self.assertIn("CHAVE", saida)

    def test_aplicar_cria_0640_com_so_o_valor(self):
        rc, _ = self.rodar(["migrar", "--aplicar"])
        self.assertEqual(rc, 0)
        arq = self.dest / "CHAVE"
        self.assertEqual(arq.read_text(), V1 + "\n")
        self.assertEqual(stat.S_IMODE(arq.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o750)

    def test_origens_divergentes_recusam_e_nao_escrevem(self):
        self.fontes["CHAVE"].append((self.t / "c.env", "CHAVE"))
        rc, saida = self.rodar(["migrar", "--aplicar"])
        self.assertEqual(rc, 1)
        self.assertFalse((self.dest / "CHAVE").exists())
        self.assertIn("diverge", saida)

    def test_segunda_rodada_e_idempotente_e_nao_sobrescreve_diferente(self):
        self.rodar(["migrar", "--aplicar"])
        (self.dest / "CHAVE").write_text(V2 + "\n")  # alguém já rodou o rodízio na fonte única
        rc, saida = self.rodar(["migrar", "--aplicar"])
        self.assertEqual(rc, 1)
        self.assertEqual((self.dest / "CHAVE").read_text(), V2 + "\n")
        self.assertIn("fonte única difere", saida)

    def test_origem_ausente_ou_sem_a_chave_e_ignorada(self):
        self.fontes["CHAVE"].append((self.t / "nao-existe", None))
        self.fontes["CHAVE"].append((self.t / "c.env", "OUTRA_CHAVE"))
        rc, _ = self.rodar(["migrar", "--aplicar"])
        self.assertEqual(rc, 0)

    def test_nenhuma_saida_tem_o_valor(self):
        self.fontes["CHAVE"].append((self.t / "c.env", "CHAVE"))
        for argv in (["auditar"], ["migrar"], ["migrar", "--aplicar"]):
            _, saida = self.rodar(argv)
            for v in (V1, V2):
                self.assertNotIn(v, saida)
        _, saida = self.rodar(["auditar"])
        from hub.comum.segredos import hash8
        self.assertIn(hash8(V1), saida)
        self.assertIn(hash8(V2), saida)

    def test_fim_de_linha_e_aspas_nao_mudam_o_hash(self):
        self.assertEqual(sf.ler_origem(self.t / "a.key", None), V1)
        self.assertEqual(sf.ler_origem(self.t / "b.env", "CHAVE"), V1)
        self.assertIsNone(sf.ler_origem(self.t / "b.env", "NAO"))


class Varrer(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        (self.t / "a.env").write_text(f"HOST=exemplo\nTOKEN_X={V1}\n")
        (self.t / "b.env").write_text(f"OUTRO_TOKEN='{V1}'\nSENHA_Y={V2}\n")
        (self.t / "solto").write_text(V2 + "\n")
        self.tr = self.t / "transc"
        self.tr.mkdir()
        (self.tr / "s.jsonl").write_text(f'{{"t":"{V1}"}}\n{{"t":"{V1}"}}\n')

    def rodar(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sf.main(argv)
        return rc, out.getvalue() + err.getvalue()

    def test_lista_nome_e_hash_sem_valor(self):
        from hub.comum.segredos import hash8
        rc, saida = self.rodar(["varrer", str(self.t)])
        self.assertEqual(rc, 0)
        for nome in ("TOKEN_X", "OUTRO_TOKEN", "SENHA_Y"):
            self.assertIn(nome, saida)
        self.assertNotIn("HOST", saida)
        self.assertIn(hash8(V1), saida)
        for v in (V1, V2):
            self.assertNotIn(v, saida)

    def test_mesmo_hash_em_dois_arquivos_vira_copia(self):
        from hub.comum.segredos import hash8
        _, saida = self.rodar(["varrer", str(self.t)])
        self.assertIn(f"{hash8(V1)}  2 origens", saida)
        self.assertIn(f"{hash8(V2)}  2 origens", saida)  # SENHA_Y em b.env e o arquivo solto

    def test_transcricoes_contam_ocorrencias_sem_valor(self):
        from hub.comum.segredos import hash8
        rc, saida = self.rodar(["varrer", str(self.t), "--transcricoes", str(self.tr)])
        self.assertEqual(rc, 0)
        self.assertRegex(saida, rf"{hash8(V1)}\s+2\s+.*s\.jsonl")
        self.assertNotIn(V1, saida)

    def test_caminho_ausente_nao_quebra(self):
        rc, _ = self.rodar(["varrer", str(self.t / "nao-existe")])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
