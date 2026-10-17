"""Portão de neutralidade: nenhum arquivo versionado (conteúdo ou caminho) pode ter os termos de tests/neutro_termos.txt.
Rodar da raiz do repo: python3 -m unittest discover -s tests -v   (ou scripts/checar.sh)."""
import subprocess
import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import neutro_lib  # noqa: E402

ISENTOS = {"tests/neutro_termos.txt"}


def arquivos_versionados():
    saida = subprocess.run(["git", "-C", str(RAIZ), "ls-files", "-z"], check=True, capture_output=True).stdout
    return [p for p in saida.decode("utf-8").split("\0") if p]


class Neutro(unittest.TestCase):
    def test_lista_de_termos_carrega(self):
        regras, permitidos = neutro_lib.carregar()
        self.assertGreater(len(regras), 10)
        self.assertIn("brk13/krohnos", permitidos)

    def test_regras_pegam_o_que_devem(self):
        regras, permitidos = neutro_lib.carregar()
        # montados por partes para o próprio teste não conter os termos
        ruins = ["ver BRU" + "-1234", "Kro" + "nos Hub", "kro" + "nos init", "BRK" + "13 pessoal", "o Bru" + "no",
                 "kro" + "hn sozinho", "Fisio " + "Gentil", "Ac" + "ta Control", "o dono " + "mediu"]
        for ruim in ruins:
            self.assertTrue(neutro_lib.achados(ruim, regras, permitidos), ruim)
        for bom in ("github.com/BRK" + "13/krohnos", "krohnos init", "Krohnos", "krohnos.com"):
            self.assertEqual(neutro_lib.achados(bom, regras, permitidos), [], bom)

    def test_nenhum_arquivo_versionado_tem_termo_proibido(self):
        regras, permitidos = neutro_lib.carregar()
        problemas = []
        arquivos = arquivos_versionados()
        self.assertGreater(len(arquivos), 5)
        for rel in arquivos:
            for d in neutro_lib.achados(rel, regras, permitidos):
                problemas.append("%s (caminho): %s" % (rel, d))
            if rel in ISENTOS:
                continue
            dados = (RAIZ / rel).read_bytes()
            if b"\0" in dados:  # binário (fonte, gif, imagem)
                continue
            try:
                texto = dados.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for d in neutro_lib.achados(texto, regras, permitidos):
                problemas.append("%s: %s" % (rel, d))
        self.assertEqual(problemas, [], "termos proibidos em arquivos versionados:\n" + "\n".join(problemas))


if __name__ == "__main__":
    unittest.main()
