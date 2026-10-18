"""Entrypoint do comando `krohnos` (script do pacote e de deploy/krohnos): krohnos init ..., krohnos demo ..."""
import sys

from hub import demo, init

COMANDOS = {"init": init.main, "demo": demo.main}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in COMANDOS:
        print("uso: krohnos init [--sim ...] | krohnos demo [--limpar]", file=sys.stderr)
        return 2
    return COMANDOS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
