#!/usr/bin/env python3
"""Agenda e fila única das tarefas do Claude da VPS. Uso e regras em hub/agenda.py (python3 deploy/agenda.py -h)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hub.agenda import main  # noqa: E402

raise SystemExit(main())
