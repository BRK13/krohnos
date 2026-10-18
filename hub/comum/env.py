"""Leitura de `.env`: uma regra só.

Linha `[export ]CHAVE=valor`; `#` no começo é comentário; valor entre aspas iguais vale o miolo (um `#` dentro das
aspas fica); sem aspas, ` # nota` no fim sai; CRLF é aceito; aspa sem par fica como está. Precedência de `segredo`:
ambiente, depois os arquivos na ordem. Nada daqui imprime valor (nem em erro).

CLI para os .sh: `python3 -m hub.comum.env get NOME [arquivo ...]` imprime o valor (sai 1 se não achar).
"""
import os
import re
import sys
from pathlib import Path

_EXPORT = re.compile(r"^export\s+")
_ASPAS = re.compile(r"""^(["'])(.*?)\1\s*(?:\s#.*)?$""")
_NOTA = re.compile(r"\s+#.*$")


def _valor(bruto):
    bruto = bruto.strip()
    m = _ASPAS.match(bruto)
    return m.group(2) if m else _NOTA.sub("", bruto).strip()


def ler_env(arquivo, estrito=False):
    """Dict {CHAVE: valor} do arquivo. Sem o arquivo: {} (ou o OSError, com `estrito=True`)."""
    try:
        texto = Path(arquivo).read_text(encoding="utf-8")
    except OSError:
        if estrito:
            raise
        return {}
    env = {}
    for linha in texto.splitlines():
        linha = _EXPORT.sub("", linha.strip())
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, bruto = linha.partition("=")
        chave = chave.strip()
        if chave and not any(c.isspace() for c in chave):
            env[chave] = _valor(bruto)
    return env


def segredo(nome, arquivos=(), ambiente=None):
    """O valor de `nome`: o ambiente ganha do arquivo; entre arquivos vale o primeiro que tem. Vazio conta como ausente."""
    amb = os.environ if ambiente is None else ambiente
    if amb.get(nome, "").strip():
        return amb[nome].strip()
    for arq in arquivos:
        valor = ler_env(arq).get(nome, "").strip()
        if valor:
            return valor
    return None


def carregar_env(arquivo, sobrescrever=False, ambiente=None):
    """Põe as chaves do arquivo no ambiente (os.environ por padrão). Por padrão não troca o que já existe.
    Devolve as chaves que gravou."""
    amb = os.environ if ambiente is None else ambiente
    gravadas = []
    for chave, valor in ler_env(arquivo).items():
        if sobrescrever or chave not in amb:
            amb[chave] = valor
            gravadas.append(chave)
    return gravadas


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if len(a) < 2 or a[0] != "get":
        print("uso: python3 -m hub.comum.env get NOME [arquivo ...]", file=sys.stderr)
        return 2
    valor = segredo(a[1], a[2:])
    if valor is None:
        return 1
    print(valor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
