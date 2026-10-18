import unittest

from hub import rastreio, rastreio_tela
from tests.test_rastreio import Base


class TestTela(Base):
    def gravar(self, passos, reg=None):
        reg = reg or self.reg
        eid = rastreio.execucao_id(reg)
        for n, p in enumerate(passos, 1):
            rastreio.gravar(eid, {"n": n, **p}, self.base)
        con = rastreio.conectar(self.base)
        with con:
            rastreio._marcar(con, reg, eid, "x", 1)
        con.close()
        return eid

    def test_tela_escapa_html_da_saida(self):
        eid = self.gravar([{"tipo": "ferramenta", "ferramenta": "Bash", "entrada": {"c": "ls"},
                            "saida": "<script>alert(1)</script>"}])
        for passo in (None, 1):
            h = rastreio_tela.tela(eid, passo, self.base)
            self.assertNotIn("<script>alert(1)</script>", h)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", h)

    def test_tela_ref_invalida_devolve_none(self):
        self.gravar([{"tipo": "fala", "texto": "oi"}])
        self.assertIsNone(rastreio_tela.tela("../x", None, self.base))
        self.assertIsNone(rastreio_tela.tela("ffff0000", None, self.base))

    def test_tela_mostra_linha_do_tempo_e_passo(self):
        eid = self.gravar([{"tipo": "fala", "texto": "vou listar"},
                           {"tipo": "ferramenta", "ferramenta": "Bash", "entrada": {"c": "ls"}, "saida": "arquivo.txt",
                            "duracao_s": 1.5},
                           {"tipo": "ferramenta", "ferramenta": "Read", "entrada": {"f": "x"}, "saida": "falhou", "erro": True}])
        h = rastreio_tela.tela(eid, None, self.base)
        self.assertIn(f"/execucao/{eid}?passo=2", h)
        self.assertIn("1.5s", h)
        self.assertIn("—", h)  # duração NULL
        self.assertIn("erro", h)
        self.assertNotIn("<h3>Saída</h3>", h)
        h = rastreio_tela.tela(eid, 2, self.base)
        self.assertIn("<h3>Saída</h3>", h)
        self.assertIn("arquivo.txt", h)
        self.assertIn("vou listar", h)  # fala anterior
        self.assertIn("não existe", rastreio_tela.tela(eid, 9, self.base))

    def test_lista_ultimas(self):
        outro = dict(self.reg, inicio="2026-01-02T10:00:00+00:00", nome="linear-krn-2")
        a = self.gravar([{"tipo": "fala", "texto": "a"}])
        b = self.gravar([{"tipo": "fala", "texto": "b"}], outro)
        h = rastreio_tela.lista(50, self.base)
        self.assertLess(h.index(b), h.index(a))  # mais recente primeiro
        self.assertEqual(h.count("/execucao/"), 2)
        self.assertIn("KRN-2", h)
        self.assertEqual(rastreio_tela.lista(1, self.base).count("/execucao/"), 1)
        self.assertIn("Nenhuma execução", rastreio_tela.lista(50, self.base / "vazio"))

    def test_limita_anteriores_com_aviso(self):
        eid = self.gravar([{"tipo": "fala", "texto": f"f{i}"} for i in range(rastreio_tela.MAX_ANTERIORES + 5)]
                          + [{"tipo": "fala", "texto": "alvo"}])
        h = rastreio_tela.tela(eid, rastreio_tela.MAX_ANTERIORES + 6, self.base)
        self.assertIn("5 omitidos", h)
        self.assertNotIn("<pre>f0</pre>", h)
        self.assertIn(f"<pre>f{rastreio_tela.MAX_ANTERIORES + 4}</pre>", h)

    def test_formulario_de_replay_no_passo(self):
        eid = self.gravar([{"tipo": "fala", "texto": "oi"}], {**self.reg, "nome": "linear-krn-7"})
        chamadas = []

        def form(acao, campos, botoes):
            chamadas.append((acao, campos, botoes))
            return f'<form action="/acao/{acao}">{campos}</form>'
        h = rastreio_tela.tela(eid, 1, self.base, formulario=form)
        self.assertIn('action="/acao/replay"', h)
        self.assertIn(f'name="ref" value="{eid}"', h)
        self.assertIn('name="passo" value="1"', h)
        self.assertIn('name="correcao"', h)
        self.assertIn("Rodar de novo deste passo com a correção", chamadas[0][2][0][0])
        self.assertIn("KRN-7", h)
        self.assertNotIn("/acao/replay", rastreio_tela.tela(eid, 1, self.base))  # sem o _form do painel: nada
        self.assertNotIn("/acao/replay", rastreio_tela.tela(eid, None, self.base, formulario=form))  # só no passo
        self.assertNotIn("/acao/replay", rastreio_tela.tela(eid, 1, self.base, formulario=lambda *a: ""))  # sem papel
        sem = self.gravar([{"tipo": "fala", "texto": "oi"}], {**self.reg, "nome": "agendada", "inicio": "2026-02-01"})
        h = rastreio_tela.tela(sem, 1, self.base, formulario=form)
        self.assertNotIn("/acao/replay", h)
        self.assertIn("não é de uma issue", h)

    def test_lista_inclui_execucao_so_em_passo(self):
        rastreio.gravar("abcd1234abcd1234", {"n": 1, "tipo": "fala", "texto": "x"}, self.base)
        a = self.gravar([{"tipo": "fala", "texto": "a"}])
        h = rastreio_tela.lista(50, self.base)
        self.assertIn("abcd1234abcd1234", h)
        self.assertLess(h.index(a), h.index("abcd1234abcd1234"))


if __name__ == "__main__":
    unittest.main()
