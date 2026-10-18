"""Migra pastas de dados e credenciais dos ids antigos para os novos (sites/<id>.json, campo ids_antigos).

    python -m hub.migrar              # mostra o plano; não muda nada
    python -m hub.migrar --aplicar    # executa, confere e grava o registro em $HUB_DATA/migracoes/

O que muda de nome (só onde o id antigo é diferente do novo):
- $HUB_DATA/aio/<antigo>/ e $HUB_DATA/geo-new/<antigo>/            → <novo>/
- $HUB_DATA/snapshots/<data>/<antigo>-{status,gsc,ga4,clarity}.json  → <novo>-...
- $NIMBUS_HOME/credenciais/clarity/<antigo>.json               → <novo>.json

Nada é apagado: arquivo é movido (mesmo disco), e qualquer conflito (destino já existe) aborta tudo antes do
primeiro movimento. Rodar de novo depois de aplicado não faz nada.
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from hub import data, home, sites

PASTAS = ("aio", "geo-new")
SUFIXOS_SNAPSHOT = ("status", "gsc", "ga4", "clarity")


def _arquivos(raiz):
    raiz = Path(raiz)
    if raiz.is_file():
        return {Path("."): raiz.stat().st_size}
    return {p.relative_to(raiz): p.stat().st_size for p in raiz.rglob("*") if p.is_file()}


def planejar(mapa, base_dados, base_home):
    """Lista de movimentos [(origem, destino)] e de conflitos (destino já existe)."""
    base_dados, base_home = Path(base_dados), Path(base_home)
    movimentos, conflitos = [], []

    def mover(origem, destino):
        if not origem.exists():
            return
        if origem.is_dir() and destino.is_dir():  # junta pastas só se nenhum arquivo colide
            for rel in _arquivos(origem):
                if (destino / rel).exists():
                    conflitos.append((origem / rel, destino / rel))
                else:
                    movimentos.append((origem / rel, destino / rel))
            return
        if destino.exists():
            conflitos.append((origem, destino))
        else:
            movimentos.append((origem, destino))

    for antigo, novo in sorted(mapa.items()):
        for pasta in PASTAS:
            mover(base_dados / pasta / antigo, base_dados / pasta / novo)
        snaps = base_dados / "snapshots"
        if snaps.is_dir():
            for dia in sorted(p for p in snaps.iterdir() if p.is_dir()):
                for sufixo in SUFIXOS_SNAPSHOT:
                    mover(dia / f"{antigo}-{sufixo}.json", dia / f"{novo}-{sufixo}.json")
        mover(base_home / "credenciais" / "clarity" / f"{antigo}.json", base_home / "credenciais" / "clarity" / f"{novo}.json")
    return movimentos, conflitos


def limpar_vazias(raiz):
    """Remove as pastas vazias dentro de uma pasta antiga (sobra de quando ela foi juntada a uma nova)."""
    raiz = Path(raiz)
    if not raiz.is_dir():
        return
    for pasta in sorted((p for p in raiz.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(pasta.iterdir()):
            pasta.rmdir()
    if not any(raiz.iterdir()):
        raiz.rmdir()


def aplicar(movimentos):
    """Move e confere: mesmo número de arquivos e mesmos tamanhos antes e depois."""
    antes = {str(o): _arquivos(o) for o, _ in movimentos}
    feitos = []
    for origem, destino in movimentos:
        destino.parent.mkdir(parents=True, exist_ok=True)
        os.replace(origem, destino)
        feitos.append({"de": str(origem), "para": str(destino)})
    for origem, destino in movimentos:
        if _arquivos(destino) != antes[str(origem)]:
            raise RuntimeError(f"conferência falhou: {destino} não bate com o que havia em {origem}")
    return feitos


def migrar(mapa, base_dados, base_home, executar=False):
    movimentos, conflitos = planejar(mapa, base_dados, base_home)
    if conflitos:
        raise RuntimeError("conflito (destino já existe), nada foi movido:\n" +
                           "\n".join(f"- {o} -> {d}" for o, d in conflitos))
    if not executar:
        return {"plano": [{"de": str(o), "para": str(d)} for o, d in movimentos], "aplicado": False}
    feitos = aplicar(movimentos)
    for antigo in mapa:
        for pasta in PASTAS:
            limpar_vazias(Path(base_dados) / pasta / antigo)
    registro = {"em": dt.datetime.now(dt.timezone.utc).isoformat(), "mapa": mapa, "movimentos": feitos, "aplicado": True}
    if feitos:
        pasta = Path(base_dados) / "migracoes"
        pasta.mkdir(parents=True, exist_ok=True)
        (pasta / f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}-ids.json").write_text(
            json.dumps(registro, ensure_ascii=False, indent=1), encoding="utf-8")
    return registro


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m hub.migrar", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--dados", help="padrão: $HUB_DATA")
    ap.add_argument("--home", help="padrão: $NIMBUS_HOME")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        r = migrar(sites.mapa_ids_antigos(), args.dados or data(), args.home or home(), args.aplicar)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    itens = r["movimentos"] if r["aplicado"] else r["plano"]
    for m in itens:
        print(f"{'movido' if r['aplicado'] else 'a mover'}: {m['de']} -> {m['para']}")
    print(f"{len(itens)} {'movimentos feitos e conferidos' if r['aplicado'] else 'movimentos no plano (use --aplicar)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
