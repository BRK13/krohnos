"""Coleta diária (collect.mjs + nimbus/so.mjs) sem rede: fetch falso no Node."""
import json
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "sem node")
class TestColeta(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    # test_gsc_sem_acesso_da_conta_de_servico_cai_no_oauth mora em tests/test_coleta_so.py (testa nimbus/so.mjs,
    # fora do snapshot público)

    def test_inativo_fora_e_clarity_sem_token_e_falta_token(self):
        raiz = self.tmp / "hub"
        (raiz / "sites").mkdir(parents=True)
        (raiz / "nimbus").mkdir()
        shutil.copy(RAIZ / "collect.mjs", raiz / "collect.mjs")
        (raiz / "nimbus/so.mjs").write_text(
            'console.log(JSON.stringify({ok:true,google:{ok:true},bing:{ok:true},ga4:{ok:true},rows:[]}));\n')
        for sid, ativo in (("clientee", True), ("corp", False)):
            (raiz / "sites" / f"{sid}.json").write_text(json.dumps(
                {"id": sid, "ativo": ativo, "dominio": f"{sid}.com", "ids": {"gsc": f"sc-domain:{sid}.com", "clarity": "abc"}}))
        (self.tmp / "so/credenciais/clarity").mkdir(parents=True)
        r = subprocess.run([NODE, str(raiz / "collect.mjs")], capture_output=True, text=True, timeout=60,
                           env={"KRONOS_PROJETOS": str(raiz / "sites"), "HUB_DATA": str(self.tmp / "dados"), "NIMBUS_HOME": str(self.tmp / "so"), "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)  # falta token não é falha de coleta
        resumo = json.loads((self.tmp / "dados/latest.json").read_text())
        self.assertEqual([p["id"] for p in resumo["projects"]], ["clientee"])
        md = (self.tmp / "dados/latest-report.md").read_text()
        self.assertIn("| clientee | OK | OK | OK | falta token |", md)
        self.assertNotIn("corp", md)

    def test_rerodar_no_mesmo_dia_refaz_status_que_falhou(self):
        # o status do ClienteE gravado de manhã (google:false) era reaproveitado e rodar a coleta de novo não corrigia.
        raiz = self.tmp / "hub"
        (raiz / "sites").mkdir(parents=True)
        (raiz / "nimbus").mkdir()
        shutil.copy(RAIZ / "collect.mjs", raiz / "collect.mjs")
        (raiz / "nimbus/so.mjs").write_text(
            'console.log(JSON.stringify({ok:true,google:{ok:true},bing:{ok:true},ga4:{ok:true},rows:[]}));\n')
        (raiz / "sites/clientee.json").write_text(json.dumps({"id": "clientee", "dominio": "clientee-com.example", "ids": {"gsc": "sc-domain:clientee-com.example"}}))
        snap = self.tmp / "dados/snapshots" / time.strftime("%Y-%m-%d", time.gmtime())
        snap.mkdir(parents=True)
        (snap / "clientee-status.json").write_text(json.dumps({"ok": False, "google": {"ok": False}, "bing": {"ok": True}, "ga4": {"ok": True}}))
        (snap / "clientee-gsc.json").write_text(json.dumps({"rows": ["salvo"]}))
        r = subprocess.run([NODE, str(raiz / "collect.mjs")], capture_output=True, text=True, timeout=60,
                           env={"KRONOS_PROJETOS": str(raiz / "sites"), "HUB_DATA": str(self.tmp / "dados"), "NIMBUS_HOME": str(self.tmp / "so"), "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads((snap / "clientee-status.json").read_text())["google"]["ok"])
        self.assertEqual(json.loads((snap / "clientee-gsc.json").read_text())["rows"], ["salvo"])  # o que deu certo segue reaproveitado


if __name__ == "__main__":
    unittest.main()
