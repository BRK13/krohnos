import re
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import roteamento as R


def m(labels=(), titulo="Ajustar tela", prio=3, cfg=None):
    return R.modelo(labels, titulo, prio, cfg)[0]


class RoteamentoTest(unittest.TestCase):
    def test_regras(self):
        self.assertEqual(m(["seo-fix"]), "haiku")
        self.assertEqual(m(["opus"]), "opus")
        self.assertEqual(m(["fable"]), "fable")
        self.assertEqual(m(["plano-x"], prio=1), "opus")
        self.assertEqual(m(["bug"], prio=1), "sonnet")  # p1 sozinha não é mais Opus
        self.assertEqual(m(["plano-x"], prio=2), "sonnet")
        self.assertEqual(m(titulo="Migração do banco"), "opus")
        self.assertEqual(m(["produto"], "busca semântica"), "opus")
        self.assertEqual(m([], "busca semântica"), "sonnet")  # sem a label produto
        self.assertEqual(m(titulo="corrigir lint"), "haiku")
        self.assertEqual(m(titulo="Issue comum"), "sonnet")

    def test_por_e_tabela(self):
        self.assertEqual(R.modelo(["opus"], "x", 3), ("opus", "label opus"))
        self.assertEqual(R.modelo([], "x", 3), ("sonnet", "padrão"))
        self.assertEqual(R.tabela()[-1], ("sonnet", "resto"))
        self.assertEqual(R.tabela({}), [("sonnet", "resto")])

    def test_cfg_vazio_e_regra_sem_condicao(self):
        self.assertEqual(m(["opus"], cfg={}), "sonnet")
        self.assertEqual(m(cfg={"regras": [{"modelo": "opus"}]}), "sonnet")

    def test_hub_json_real(self):
        cfg = R.config()
        self.assertTrue(cfg["regras"])
        for r in cfg["regras"]:
            re.compile(r.get("titulo", ""))
        self.assertEqual(cfg["retomada_deploy"], "haiku")

    def test_regex_invalido_ignora_a_regra(self):
        cfg = {"regras": [{"modelo": "opus", "titulo": "(["}, {"modelo": "haiku", "titulo": "lint"}]}
        self.assertEqual(m(titulo="corrigir lint", cfg=cfg), "haiku")
        self.assertEqual(m(titulo="outra", cfg=cfg), "sonnet")

    def test_roteamento_que_nao_e_dict(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "hub.json").write_text(json.dumps({"roteamento": ["x"]}), encoding="utf-8")
            with mock.patch.dict("os.environ", {"KROHNOS_HUB_JSON": str(Path(d) / "hub.json")}):
                self.assertEqual(R.config(), {})
