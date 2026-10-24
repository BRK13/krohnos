import json
import os
import re
import subprocess
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LITERAIS = ("/var/lib/nimbus", "/opt/nimbus", "/etc/nimbus", "/home/nimbus")
# hub/config.py guarda o legado da nossa VPS (LEGADO): único lugar que pode ter os literais.
PERMITIDOS = {"hub/config.py", "hub/comum/maquinas.py"}  # maquinas.py: o /etc/nimbus é o arquivo do IP
# identidade e negócio nunca no código de hub/: nome do dono, org do GitHub e a marca vêm de config.get(...).
# hub/marca/ só tem SVG (não é .py). reels_fsite06.py (nomes da equipe) já saiu: é do plugin krohnos_plugin_reels.
IDENTIDADE = ("Fulano", "example-org", "Krohnos")
# hub/licoes.py minera a fala do dono (regex sobre "Fulano" e o dono do GitHub): legado a migrar para config.dono().
PERMITIDOS_IDENTIDADE = {"hub/config.py": IDENTIDADE, "hub/licoes.py": ("Fulano", "example-org"), "hub/wiki.py": IDENTIDADE}  # wiki: do repo privado, fora do snapshot
# Fora da varredura da lista privada: exemplos fictícios, planos históricos e vendor.
IGNORADOS = ("exemplos/", "docs/plans/", "vendor/", "privado/", "tests/test_neutro.py")
# Únicos usos legítimos: a URL do repo público do produto (example-org/krohnos) e o saneador do snapshot, que precisa do termo
# para trocá-lo (a regra em publico.proibidos e os testes dele).
URL_PUBLICA = re.compile(r"github\.com/example-org/krohnos(?![\w-])", re.I)
DO_SANEADOR = ("publico.proibidos", "scripts/snapshot_publico.py", "tests/test_saneamento_publico.py", "tests/test_snapshot.py")
# Apelidos da topologia: lista versionada, lida sempre (aqui e no repo público); a própria lista não é varrida.
APELIDOS = RAIZ / "tests" / "neutro_apelidos.txt"
# site/ só existe no repo público e não passa pelo saneamento: chave de issue interna lá é vazamento cru. O prefixo vem
# partido de propósito: o saneamento troca o prefixo inteiro no código e esta linha deixaria de procurar o que procura.
PREFIXO_ISSUE = "B" "RU"


def proibidos():
    """Linhas de $KROHNOS_PROIBIDOS (arquivo do repo privado: nomes, org, domínios, IDs dos nossos sites); vazio sem a variável."""
    caminho = os.environ.get("KROHNOS_PROIBIDOS", "")
    if not caminho or not Path(caminho).is_file():
        return []
    return [l.strip() for l in Path(caminho).read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]


def apelidos():
    """Termos de tests/neutro_apelidos.txt (um por linha, # é comentário)."""
    try:
        linhas = APELIDOS.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [l.strip() for l in linhas if l.strip() and not l.lstrip().startswith("#")]


def _com_borda(termos):
    return re.compile("|".join(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])" for t in termos), re.I) if termos else None


def _linhas(rel):
    try:
        return (RAIZ / rel).read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
        return []


def rastreados():
    try:
        saida = subprocess.run(["git", "ls-files", "-z"], cwd=RAIZ, capture_output=True, check=True).stdout.decode()
        arquivos = [a for a in saida.split("\0") if a]
    except (subprocess.CalledProcessError, FileNotFoundError):  # pasta do snapshot, ainda sem git
        arquivos = [str(p.relative_to(RAIZ)) for p in RAIZ.rglob("*") if p.is_file() and ".git" not in p.parts]
    return [a for a in arquivos if not a.startswith(IGNORADOS) and a not in DO_SANEADOR]


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
        """Com $KROHNOS_PROIBIDOS, nenhum arquivo rastreado passa da contagem de tests/neutro_base.json (só desce: ao
        zerar uma pasta, apague a linha; arquivo fora da base tem teto 0)."""
        termos = proibidos()
        if not termos:
            self.skipTest("sem $KROHNOS_PROIBIDOS: só os testes genéricos")
        base = json.loads((RAIZ / "tests" / "neutro_base.json").read_text(encoding="utf-8"))
        achados = []
        for a in rastreados():
            try:
                linhas = (RAIZ / a).read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            n = sum(1 for linha in linhas if any(t.lower() in URL_PUBLICA.sub("", linha).lower() for t in termos))
            if n > base.get(a, 0):
                achados.append(f"{a}: {n} linhas com termo da lista privada (teto {base.get(a, 0)})")
        self.assertEqual(achados, [], "termo da lista privada no repositório aberto")

    def test_sem_apelidos_da_topologia(self):
        """Nenhum arquivo rastreado (site/ inclusive) passa do teto de apelidos de tests/neutro_base.json → "apelidos"
        (só desce: ao zerar um arquivo, apague a linha; arquivo fora da base tem teto 0)."""
        termos = apelidos()
        self.assertIn("servidor_a", termos)
        rx = _com_borda(termos)
        teto = json.loads((RAIZ / "tests" / "neutro_base.json").read_text(encoding="utf-8")).get("apelidos", {})
        achados = []
        for a in rastreados():
            if a in ("tests/neutro_apelidos.txt", "tests/neutro_base.json"):  # a lista e o teto (que cita os caminhos)
                continue
            n = sum(1 for linha in _linhas(a) if rx.search(URL_PUBLICA.sub("", linha)))
            if n > teto.get(a, 0):
                achados.append(f"{a}: {n} linhas com apelido da topologia (teto {teto.get(a, 0)})")
        self.assertEqual(achados, [], "apelido do saneamento desenha a nossa operação: tire do código (config/cadastro)")

    def test_site_sem_chave_de_issue(self):
        """site/ (repo público, fora do snapshot e do saneamento): nenhuma chave de issue interna, nem em teste nem em doc."""
        rx = re.compile(rf"\b{PREFIXO_ISSUE}-\d+", re.I)
        achados = [f"{a}:{n}" for a in rastreados() if a.startswith("site/")
                   for n, linha in enumerate(_linhas(a), 1) if rx.search(linha)]
        self.assertEqual(achados, [], "chave de issue interna em site/")


if __name__ == "__main__":
    unittest.main()
