"""Entrypoint do comando `kronos` (script do pacote e de deploy/kronos): kronos init ..., kronos demo ..."""
import sys

from hub import demo, init

COMANDOS = {"init": init.main, "demo": demo.main}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMANDOS:
        print("uso: kronos init [--sim ...] | kronos demo [--limpar]", file=sys.stderr)
        return 2
    return COMANDOS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
