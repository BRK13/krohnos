"""python3 -m hub.x_salvos coletar [--forcar] [--config x_salvos.toml] [--jitter] [--refazer-fio AAAA-MM-DD.jsonl]
python3 -m hub.x_salvos extrair   (receita a partir do jsonl do dia)"""
import argparse
import random
import sys
import time

from . import config as C
from . import guardrails as G


def parser():
    ap = argparse.ArgumentParser(prog="python3 -m hub.x_salvos", add_help=False, epilog=G.AVISO_TERMOS,
                                 formatter_class=argparse.RawDescriptionHelpFormatter, description="Agente de conhecimento do X: salvos -> coleta -> receita.")
    ap.add_argument("-h", "--help", "--ajuda", action="help", help="mostra esta ajuda e o aviso de termos")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for nome in ("coletar", "extrair"):
        s = sub.add_parser(nome, add_help=False, epilog=G.AVISO_TERMOS, formatter_class=argparse.RawDescriptionHelpFormatter)
        s.add_argument("-h", "--help", "--ajuda", action="help")
        s.add_argument("--config", help="x_salvos.toml (ou KROHNOS_X_SALVOS_CONFIG)")
        if nome == "coletar":
            s.add_argument("--forcar", action="store_true", help="ignora o limite de uma coleta por dia")
            s.add_argument("--jitter", action="store_true", help="espera de 0 a jitter_max_s antes de começar")
            s.add_argument("--refazer-fio", metavar="ARQ", help="relê o fio dos salvos de um .jsonl já coletado")
    return ap


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        cfg = C.carregar(a.config)
    except C.ConfigInvalida as e:
        print(f"configuração inválida: {e}", file=sys.stderr)
        return 1
    if a.cmd == "extrair":
        from . import extrair
        return extrair.main(cfg)
    from . import coletor
    trava = coletor.travar(cfg.trava)
    if trava is None:
        G.log("outra coleta já está rodando")
        return 0
    try:
        if a.jitter:
            time.sleep(random.uniform(0, cfg.jitter_max_s))
        if a.refazer_fio:
            return coletor.refazer_fio(cfg, a.refazer_fio)
        return coletor.coletar(cfg, forcar=a.forcar)
    except G.CookiesInvalidos as e:
        coletor.gravar_erro(cfg.saida, f"cookies inválidos: {e}")
        return 2
    except Exception as e:  # noqa: BLE001 — nada de traceback com valor: log mascarado, ERRO e código 2
        G.log_erro(e)
        coletor.gravar_erro(cfg.saida, f"erro inesperado: {G.descrever_erro(e)}")
        return 2
    finally:
        trava.close()


if __name__ == "__main__":
    sys.exit(main())
