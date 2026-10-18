"""Auditoria e migração da fonte única de segredos (/F7). Nunca imprime valor: só nome, origem e hash8.

    python3 -m hub.segredos_fonte auditar            # cada segredo: origens, hash8, se já está na fonte única
    python3 -m hub.segredos_fonte migrar [--aplicar] # copia para <hub.segredos()>/segredos/<NOME> (sem --aplicar: só mostra)
    python3 -m hub.segredos_fonte varrer <caminho>... [--transcricoes <pasta>]
        # root, segunda passada: nome e hash8 de tudo com cara de segredo nos arquivos, quais hashes repetem em mais de um
        # arquivo (cópia) e, nas transcrições (*.jsonl), quantas vezes cada valor aparece (só contagem)

A migração é um root rodando isto por Pedido `vps`. Recusa (código 1) se as origens de um nome têm valores diferentes
ou se a fonte única já tem outro valor (rodízio feito lá vence: nunca é sobrescrita). Não apaga nenhuma origem.
"""
import os
import re
import sys
from pathlib import Path

import hub
from hub.comum.segredos import DIR_PADRAO, hash8

HOME = hub.home_agentes()
ETC = hub.segredos()
# nome -> origens (arquivo, chave dentro do arquivo | None = o arquivo inteiro é o valor). Só fontes de arquivo do hub.
FONTES = {
    "OPENROUTER_API_KEY": [(HOME / ".config/openrouter.key", None),
                           (ETC / "openrouter.env", "OPENROUTER_API_KEY")],
    "RESEND_API_KEY": [(ETC / "apps/asite01.env", "RESEND_API_KEY"),
                       (ETC / "apps/fsite05.env", "RESEND_API_KEY")],
    "LINEAR_API_KEY": [(ETC / "linear.env", "LINEAR_API_KEY")],
    "PAGESPEED_CHAVE": [(ETC / "pagespeed.env", "PAGESPEED_CHAVE")],
    "ELEVENLABS_API_KEY": [(HOME / ".config/elevenlabs.key", None), (HOME / ".config/elevenlabs", None)],
}
GRUPO = "claude"  # o hub roda como claude; o grupo por consumidor entra aqui quando houver outro consumidor
_LINHA = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def ler_origem(arquivo, chave):
    """Valor de uma origem, ou None (arquivo ausente, ilegível ou sem a chave)."""
    try:
        texto = Path(arquivo).read_text(encoding="utf-8")
    except OSError:
        return None
    if chave is None:
        return texto.strip() or None
    for linha in texto.splitlines():
        m = _LINHA.match(linha)
        if m and m.group(1) == chave and not linha.lstrip().startswith("#"):
            return m.group(2).strip("'\"") or None
    return None


def _levantar(nome, fontes, destino):
    """(valores {hash8: valor}, origens [(arquivo, hash8)], hash8 da fonte única ou None). O valor nunca sai daqui."""
    valores, origens = {}, []
    for arq, chave in fontes[nome]:
        v = ler_origem(arq, chave)
        if v:
            valores[hash8(v)] = v
            origens.append((str(arq), hash8(v)))
    atual = ler_origem(Path(destino) / nome, None)
    return valores, origens, (hash8(atual) if atual else None)


def auditar(fontes, destino):
    print(f"{'NOME':22} {'ORIGENS':>7} {'HASHES':>6}  fonte única")
    for nome in fontes:
        valores, origens, atual = _levantar(nome, fontes, destino)
        print(f"{nome:22} {len(origens):>7} {len(valores):>6}  {atual or '-'}")
        for arq, h in origens:
            print(f"    {h}  {arq}")
    return 0


def migrar(fontes, destino, aplicar, grupo):
    rc = 0
    for nome in fontes:
        valores, origens, atual = _levantar(nome, fontes, destino)
        if not valores:
            print(f"{nome}: sem origem legível (nada a migrar)")
        elif len(valores) > 1:
            print(f"{nome}: diverge ({', '.join(sorted(valores))}); resolva antes, nada escrito")
            rc = 1
        else:
            (h, v), = valores.items()
            if atual == h:
                print(f"{nome}: já na fonte única ({h})")
            elif atual:
                print(f"{nome}: fonte única difere das origens (fonte {atual}, origens {h}); mantida")
                rc = 1
            elif not aplicar:
                print(f"{nome}: seria criado ({h}, {len(origens)} origem(ns))")
            else:
                _gravar(Path(destino), nome, v, grupo)
                print(f"{nome}: criado ({h})")
    return rc


def _gravar(destino, nome, valor, grupo):
    destino.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chmod(destino, 0o750)
    arq = destino / nome
    fd = os.open(arq, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "w") as f:
        f.write(valor + "\n")
    os.chmod(arq, 0o640)
    if grupo:
        import shutil
        shutil.chown(destino, group=grupo)
        shutil.chown(arq, group=grupo)


_NOME_SEGREDO = re.compile(r"(KEY|TOKEN|PASS|SENHA|SECRET|PAT)", re.I)


def _arquivos(caminhos):
    for c in caminhos:
        p = Path(c)
        if p.is_dir():
            yield from (f for f in sorted(p.rglob("*")) if f.is_file())
        elif p.is_file():
            yield p


def _segredos_do_arquivo(arq):
    """[(nome, valor)] de um arquivo: linhas NOME=valor com nome de segredo, ou o arquivo solto de uma linha só."""
    try:
        if arq.stat().st_size > 64_000:
            return []
        texto = arq.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    achados = []
    for linha in texto.splitlines():
        m = _LINHA.match(linha)
        if m and not linha.lstrip().startswith("#") and _NOME_SEGREDO.search(m.group(1)):
            v = m.group(2).strip("'\"")
            if v:
                achados.append((m.group(1), v))
    if not achados and len(texto.strip().splitlines()) == 1 and "=" not in texto and len(texto.strip()) >= 8:
        achados.append((arq.name, texto.strip()))
    return achados


def varrer(caminhos, transcricoes):
    por_hash, valores = {}, {}
    for arq in _arquivos(caminhos):
        for nome, v in _segredos_do_arquivo(arq):
            h = hash8(v)
            valores[h] = v
            por_hash.setdefault(h, []).append((str(arq), nome))
            print(f"{h}  {nome:30} {arq}")
    print("\nCÓPIAS (mesmo valor em mais de um lugar)")
    for h, orig in sorted(por_hash.items()):
        if len(orig) > 1:
            print(f"{h}  {len(orig)} origens")
            for arq, nome in orig:
                print(f"    {nome}  {arq}")
    if transcricoes:
        print("\nTRANSCRIÇÕES (hash8, ocorrências, arquivo)")
        for arq in _arquivos([transcricoes]):
            try:
                dados = arq.read_bytes()
            except OSError:
                continue
            for h, v in sorted(valores.items()):
                n = dados.count(v.encode())
                if n:
                    print(f"{h}  {n:>4}  {arq}")
    return 0


def main(argv, fontes=None, destino=None, grupo=GRUPO):
    fontes = FONTES if fontes is None else fontes
    destino = Path(destino or os.environ.get("NIMBUS_SEGREDOS") or DIR_PADRAO)
    if argv[:1] == ["auditar"]:
        return auditar(fontes, destino)
    if argv[:1] == ["migrar"]:
        return migrar(fontes, destino, "--aplicar" in argv, grupo)
    if argv[:1] == ["varrer"]:
        resto = argv[1:]
        tr = None
        if "--transcricoes" in resto:
            i = resto.index("--transcricoes")
            tr = resto[i + 1] if i + 1 < len(resto) else None
            resto = resto[:i] + resto[i + 2:]
        return varrer(resto, tr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
