#!/usr/bin/env python3
"""Agenda e fila única das tarefas do Claude da VPS. Uso e regras em hub/agenda.py (python3 deploy/agenda.py -h)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trava_previa  # noqa: E402 (só stdlib: a trava vem antes de qualquer import do hub)

_previa = trava_previa.pegar(sys.argv)
if _previa is False:
    raise SystemExit(0)  # outro tick desta conta/tipo/linha ainda coordena: sai sem carregar nada

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hub import agenda  # noqa: E402

agenda.TRAVA_PREVIA = _previa
raise SystemExit(agenda.main())
