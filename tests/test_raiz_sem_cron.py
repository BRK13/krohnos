"""Sem o crontab do sistema (a instalação pública não traz deploy/nimbus.cron), rotinas() devolve vazio, sem erro."""
import tempfile
import unittest
from pathlib import Path

from hub import raiz


class SemCronTest(unittest.TestCase):
    def test_cron_inexistente_vira_vazio(self):
        with tempfile.TemporaryDirectory() as pasta:
            self.assertEqual(raiz.rotinas(Path(pasta) / "nao-existe.cron"), {})
