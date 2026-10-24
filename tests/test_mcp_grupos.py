import types
import unittest
from unittest import mock

from hub import mcp_grupos, mcp_proxy


def _grupo(nome_modulo, *ferramentas, resposta=None):
    m = types.ModuleType(nome_modulo)
    m.FERRAMENTAS = {n: {"description": n, "inputSchema": {"type": "object", "properties": {}}} for n in ferramentas}
    m.executar = lambda nome, args, site: resposta or {"ok": True, "nome": nome, "site": site and site["id"]}
    return m


class GruposTest(unittest.TestCase):
    def test_juntar_aponta_cada_ferramenta_ao_modulo(self):
        a, b = _grupo("a", "x", "y"), _grupo("b", "z")
        self.assertEqual(mcp_grupos.juntar([a, b]), {"x": a, "y": a, "z": b})

    def test_nome_repetido_entre_grupos_falha(self):
        with self.assertRaises(RuntimeError):
            mcp_grupos.juntar([_grupo("a", "x"), _grupo("b", "x")])

    def test_modulo_com_nome_de_ajudante_falha(self):
        with mock.patch.object(mcp_grupos.pkgutil, "iter_modules", return_value=[types.SimpleNamespace(name="pasta")]), \
                mock.patch.object(mcp_grupos.importlib, "import_module") as imp, self.assertRaises(RuntimeError):
            mcp_grupos.carregar()
        imp.assert_not_called()

    def test_ajudantes_sobrevivem_a_carga_dos_grupos(self):
        mcp_grupos.carregar()
        for nome in ("local", "pasta", "inteiro", "palavras", "alvo", "so"):
            self.assertTrue(callable(getattr(mcp_grupos, nome)), nome)

    def test_carregar_nao_repete_o_nucleo(self):
        nucleo = set(mcp_proxy.FERRAMENTAS) - set(mcp_proxy.GRUPOS)
        self.assertFalse(nucleo & set(mcp_grupos.carregar()))

    def test_executar_manda_pro_grupo_com_o_site_resolvido(self):
        g = _grupo("g", "ferramenta_de_teste")
        with mock.patch.dict(mcp_proxy.GRUPOS, {"ferramenta_de_teste": g}), \
             mock.patch.dict(mcp_proxy.FERRAMENTAS, {"ferramenta_de_teste": g.FERRAMENTAS["ferramenta_de_teste"]}):
            r = mcp_proxy.executar("ferramenta_de_teste", {"site": "psite15"})
            sem_site = mcp_proxy.executar("ferramenta_de_teste", {})
        self.assertEqual(r, {"ok": True, "nome": "ferramenta_de_teste", "site": "psite15"})
        self.assertEqual(sem_site["site"], None)

    def test_pasta_fica_em_hub_data_mcp(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"HUB_DATA": d}):
            p = mcp_grupos.pasta("relatorios", "psite15")
            self.assertTrue(p.is_dir())
            self.assertEqual(p.relative_to(d).as_posix(), "mcp/relatorios/psite15")

    def test_inteiro_e_palavras(self):
        self.assertEqual(mcp_grupos.inteiro({"n": "500"}, "n", 10, 100), 100)
        with self.assertRaises(mcp_grupos.ErroUso):
            mcp_grupos.palavras({"keywords": []}, 10)


if __name__ == "__main__":
    unittest.main()
