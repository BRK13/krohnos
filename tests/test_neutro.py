import json
import os
import subprocess
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LITERAIS = ("/var/lib/nimbus", "/opt/nimbus", "/etc/nimbus", "/home/nimbus")
# hub/config.py guarda o legado da nossa VPS (LEGADO): único lugar que pode ter os literais.
PERMITIDOS = {"hub/config.py"}
# identidade e negócio nunca no código de hub/: nome do dono, org do GitHub e a marca vêm de config.get(...).
# hub/marca/ só tem SVG (não é .py). reels_fsite05.py (nomes da equipe) já saiu: é do plugin kronos_plugin_reels.
IDENTIDADE = ("Fulano", "example-org", "Kronos")
# hub/licoes.py minera a fala do dono (regex sobre "Fulano" e o dono do GitHub): legado a migrar para config.dono().
PERMITIDOS_IDENTIDADE = {"hub/config.py": IDENTIDADE, "hub/licoes.py": ("Fulano", "example-org"), "hub/wiki.py": IDENTIDADE}  # wiki: do repo privado, fora do snapshot
# Fora da varredura da lista privada: exemplos fictícios, planos históricos e vendor.
IGNORADOS = ("exemplos/", "docs/plans/", "vendor/", "privado/", "tests/test_neutro.py")


def proibidos():
    """Linhas de $KRONOS_PROIBIDOS (arquivo do repo privado: nomes, org, domínios, IDs dos nossos sites); vazio sem a variável."""
    caminho = os.environ.get("KRONOS_PROIBIDOS", "")
    if not caminho or not Path(caminho).is_file():
        return []
    return [l.strip() for l in Path(caminho).read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]


def rastreados():
    saida = subprocess.run(["git", "ls-files", "-z"], cwd=RAIZ, capture_output=True, check=True).stdout.decode()
    return [a for a in saida.split("\0") if a and not a.startswith(IGNORADOS)]


class TestNeutro(unittest.TestCase):
    def test_hub_sem_caminho_fixo(self):
        arquivos = sorted(str(p.relative_to(RAIZ)) for p in (RAIZ / "hub").rglob("*.py"))
        self.assertTrue(arquivos)
        achados = [f"{a}:{n}: {lit}" for a in arquivos if a not in PERMITIDOS
                   for n, linha in enumerate((RAIZ / a).read_text(encoding="utf-8").splitlines(), 1)
                   for lit in LITERAIS if lit in linha]
        self.assertEqual(achados, [], "caminho fixo em hub/: use hub.data()/home()/codigo()/segredos()/home_agentes()")

    def test_hub_sem_identidade_nem_marca(self):
        achados = [f"{p.relative_to(RAIZ)}:{n}: {lit}" for p in sorted((RAIZ / "hub").glob("*.py"))
                   for n, linha in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                   for lit in IDENTIDADE if lit in linha and lit not in PERMITIDOS_IDENTIDADE.get(str(p.relative_to(RAIZ)), ())]
        self.assertEqual(achados, [], "use config.dono()/config.repo()/config.get('produto.nome')")

    def test_sem_proibidos_da_lista_privada(self):
        """Com $KRONOS_PROIBIDOS, nenhum arquivo rastreado passa da contagem de tests/neutro_base.json (só desce: ao
        zerar uma pasta, apague a linha; arquivo fora da base tem teto 0)."""
        termos = proibidos()
        if not termos:
            self.skipTest("sem $KRONOS_PROIBIDOS: só os testes genéricos")
        base = json.loads((RAIZ / "tests" / "neutro_base.json").read_text(encoding="utf-8"))
        achados = []
        for a in rastreados():
            try:
                linhas = (RAIZ / a).read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            n = sum(1 for linha in linhas if any(t.lower() in linha.lower() for t in termos))
            if n > base.get(a, 0):
                achados.append(f"{a}: {n} linhas com termo da lista privada (teto {base.get(a, 0)})")
        self.assertEqual(achados, [], "termo da lista privada no repositório aberto")


if __name__ == "__main__":
    unittest.main()
