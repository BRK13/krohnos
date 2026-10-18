"""Leitura de tests/neutro_termos.txt e busca dos termos. Só stdlib; usado por test_neutro.py e site/tests/test_build.py."""
import re
from pathlib import Path

TERMOS = Path(__file__).resolve().parent / "neutro_termos.txt"


def carregar(caminho=TERMOS):
    """Devolve (regras, permitidos): regras = [(descricao, regex compilada)], permitidos = [texto]."""
    regras, permitidos = [], []
    for linha in Path(caminho).read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        if linha.startswith("allow:"):
            permitidos.append(linha[6:].lower())
        elif linha.startswith("re:"):
            regras.append((linha, re.compile(linha[3:], re.I)))
        else:
            regras.append((linha, re.compile(re.escape(linha), re.I)))
    return regras, permitidos


def achados(texto, regras, permitidos):
    """Lista de descrições das regras que batem em `texto`."""
    baixo = texto.lower()
    for p in permitidos:
        baixo = baixo.replace(p, " ")
    return [d for d, rx in regras if rx.search(baixo)]
