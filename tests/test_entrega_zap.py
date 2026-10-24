"""toda entrega visual vai sozinha ao WhatsApp do dono, sem duplicar, respeitando os limites."""
import datetime as dt, json, tempfile, unittest
from pathlib import Path
from unittest import mock
from hub import entrega_zap as ez

AGORA = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.timezone.utc)


def issue(corpo="", anexos=(), ident="KRN-900", titulo="Reel novo da ClienteI"):
    return {"id": "i", "identifier": ident, "title": titulo, "state": {"name": "Done"},
            "attachments": {"nodes": [{"url": u, "title": t} for u, t in anexos]},
            "comments": {"nodes": [{"createdAt": "2026-10-16T11:00:00Z", "user": {"app": True}, "body": corpo}]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        raiz = Path(self.tmp.name)
        self.dados, self.bot = raiz / "dados", raiz / "bot"
        self.caixa = self.bot / "avisos"
        self.caixa.mkdir(parents=True)
        self.dados.mkdir()
        self.saida = raiz / "saida"
        self.saida.mkdir()
        self.enviados = []

    def arq(self, nome, tam=10, conteudo=None):
        p = self.saida / nome
        p.write_bytes(conteudo if conteudo is not None else nome.encode().ljust(tam, b"x"))
        return p

    def avisar(self, texto, **kw):
        self.enviados.append((texto, kw))

    def rodar(self, i, **kw):
        with mock.patch.object(ez, "admin_numero", return_value="5500000000002"), \
                mock.patch.object(ez, "_hosts_dos_sites", return_value={"site-b.exemplo.com"}):
            return ez.entregar(i, str(self.dados), str(self.caixa), self.avisar, agora=AGORA, **kw)


class Coleta(Base):
    def test_caminho_citado_no_ultimo_comentario(self):
        p = self.arq("reel.mp4")
        with mock.patch.object(ez, "_hosts_dos_sites", return_value=set()):
            c = ez.candidatos(issue(f"Pronto: {p} e /tmp/nao-existe.pdf"), str(self.dados))
        self.assertEqual([("arquivo", "reel.mp4")], [(x["tipo"], x["nome"]) for x in c])

    def test_codigo_e_json_ficam_fora(self):
        p, q = self.arq("dados.json"), self.arq("script.py")
        with mock.patch.object(ez, "_hosts_dos_sites", return_value=set()):
            self.assertEqual([], ez.candidatos(issue(f"PR pronto, {p} {q} https://github.com/acme/x/pull/3"), str(self.dados)))

    def test_anexo_do_linear_e_previa(self):
        u = "https://uploads.linear.app/o/a/b"
        c = ez.candidatos(issue("Prévia: https://site-abc.vercel.app/x", [(u, "proposta.pdf"), (u + "2", "log.txt")]), str(self.dados))
        self.assertEqual({("linear", "proposta.pdf"), ("link", "https://site-abc.vercel.app/x")},
                         {(x["tipo"], x["nome"]) for x in c})

    def test_pagina_do_site_so_vale_como_pagina_nova(self):
        with mock.patch.object(ez, "_hosts_dos_sites", return_value={"site-b.exemplo.com"}):
            self.assertEqual(1, len(ez.candidatos(issue("Página nova no ar: https://site-b.exemplo.com/dor-lombar"), str(self.dados))))
            self.assertEqual([], ez.candidatos(issue("Ajustei o título de https://site-b.exemplo.com/dor-lombar"), str(self.dados)))

    def test_registro_do_anexar_com_arquivo_local(self):
        p = self.arq("a.pdf")
        (self.dados / "claude" / "anexos").mkdir(parents=True)
        (self.dados / "claude" / "anexos" / "KRN-900.jsonl").write_text(
            json.dumps({"link": "[a.pdf](https://uploads.linear.app/x/y/z)", "arquivo": str(p)}) + "\n")
        c = ez.candidatos(issue(""), str(self.dados))
        self.assertEqual([("arquivo", str(p))], [(x["tipo"], x["origem"]) for x in c])


class Envio(Base):
    def test_manda_com_para_e_uma_linha(self):
        p = self.arq("proposta.pdf")
        r = self.rodar(issue(f"Feito: {p}"))
        texto, kw = self.enviados[0]
        self.assertEqual("KRN-900 pronta: Reel novo da ClienteI", texto)
        self.assertEqual("5500000000002", kw["para"])
        self.assertEqual(["envios/entregas/KRN-900/proposta.pdf"], kw["arquivos"])
        self.assertTrue((self.bot / "envios/entregas/KRN-900/proposta.pdf").is_file())
        self.assertEqual(["proposta.pdf"], r["enviados"])

    def test_nao_repete(self):
        p = self.arq("proposta.pdf")
        self.rodar(issue(f"Feito: {p}"))
        r = self.rodar(issue(f"Feito: {p}"))
        self.assertEqual(1, len(self.enviados))
        self.assertEqual(1, r["ja"])

    def test_mesmo_conteudo_duas_vezes_vai_uma(self):
        a, b = self.arq("a.png", conteudo=b"igual"), self.arq("b.png", conteudo=b"igual")
        self.rodar(issue(f"{a} {b}"))
        self.assertEqual(["envios/entregas/KRN-900/a.png"], self.enviados[0][1]["arquivos"])

    def test_mesmo_nome_conteudo_diferente_nao_se_sobrescrevem(self):
        a = self.arq("carteira.png", conteudo=b"um")
        b = self.saida / "outra"
        b.mkdir()
        (b / "carteira.png").write_bytes(b"dois")
        self.rodar(issue(f"{a} {b / 'carteira.png'}"))
        rels = self.enviados[0][1]["arquivos"]
        self.assertEqual(2, len(rels))  # direto, sem zip; nomes distintos
        self.assertEqual({b"um", b"dois"}, {(self.bot / r).read_bytes() for r in rels})

    def test_issue_ja_entregue_nao_vai_de_novo_nem_com_arquivo_novo(self):
        p = self.arq("p.pdf", conteudo=b"v1")
        self.rodar(issue(f"{p}"))
        self.arq("p.pdf", conteudo=b"v2")
        self.rodar(issue(f"{p}"))
        self.assertEqual(1, len(self.enviados))  # Fulano, 16/10: em loop

    def test_agente_ja_mandou(self):
        p = self.arq("reel.mp4")
        (self.bot / "reels").mkdir()
        (self.bot / "reels" / "reel.mp4").write_bytes(p.read_bytes())
        (self.caixa / "enviados").mkdir()
        (self.caixa / "enviados" / "x.json").write_text(json.dumps(
            {"texto": "KRN-900 reel pronto", "arquivos": ["reels/reel.mp4"]}))
        r = self.rodar(issue(f"{p}"))
        self.assertEqual([], self.enviados)
        self.assertEqual(1, r["ja"])

    def test_aviso_de_outra_issue_nao_conta(self):
        p = self.arq("reel.mp4")
        (self.bot / "reels").mkdir()
        (self.bot / "reels" / "reel.mp4").write_bytes(p.read_bytes())
        (self.caixa / "enviados").mkdir()
        (self.caixa / "enviados" / "x.json").write_text(json.dumps({"texto": "KRN-901 reel", "arquivos": ["reels/reel.mp4"]}))
        self.rodar(issue(f"{p}"))
        self.assertEqual(1, len(self.enviados))

    def test_so_codigo_nao_manda_nada(self):
        r = self.rodar(issue("PR #12 mergeado, testes verdes, no ar."))
        self.assertEqual([], self.enviados)
        self.assertIsNone(r["texto"])

    def test_link_de_previa_vai_no_texto_e_nao_repete(self):
        i = issue("Prévia: https://x-123.vercel.app/")
        self.rodar(i)
        self.assertIn("https://x-123.vercel.app/", self.enviados[0][0])
        self.assertEqual([], self.enviados[0][1]["arquivos"])
        self.rodar(i)
        self.assertEqual(1, len(self.enviados))

    def test_baixa_do_linear(self):
        def baixar(url, destino):
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(b"%PDF-1")
            return destino
        with mock.patch.object(ez, "_baixar", side_effect=baixar) as b:
            i = issue("", [("https://uploads.linear.app/o/a/b", "proposta.pdf")])
            self.rodar(i)
            self.rodar(i)
        self.assertEqual(1, b.call_count)  # a 2ª rodada já conhece o hash da URL
        self.assertEqual(1, len(self.enviados))

    def test_simular_nao_grava_nem_manda(self):
        p = self.arq("a.pdf")
        r = self.rodar(issue(f"{p}"), simular=True)
        self.assertEqual(["a.pdf"], r["enviados"])
        self.assertEqual([], self.enviados)
        self.assertFalse((self.bot / "envios").exists())


class Limites(Base):
    def test_ate_4_arquivos_vao_diretos(self):
        ps = [self.arq("a.pdf"), self.arq("b.pdf"), self.arq("c.mp4"), self.arq("d.docx")]
        self.rodar(issue(" ".join(map(str, ps))))
        texto, kw = self.enviados[0]
        self.assertEqual(4, len(kw["arquivos"]))
        self.assertFalse(any(a.endswith(".zip") for a in kw["arquivos"]))
        self.assertEqual(1, len(texto.splitlines()))

    def test_mais_de_4_imagens_viram_mosaico_e_pdf_vai_direto(self):
        from PIL import Image
        ps = []
        for i in range(8):
            p = self.saida / f"p{i}.png"
            Image.new("RGB", (100, 100), (i * 30, 0, 0)).save(p)
            ps.append(p)
        ps.append(self.arq("proposta.pdf"))
        self.rodar(issue(" ".join(map(str, ps))))
        rels = self.enviados[0][1]["arquivos"]
        self.assertEqual(2, len(rels))
        self.assertTrue(any(r.endswith("KRN-900-previas.jpg") for r in rels))
        self.assertFalse(any(r.endswith(".zip") for r in rels))

    def test_mais_de_4_pdfs_mandam_so_4(self):
        ps = [self.arq(f"d{i}.pdf", conteudo=bytes([i])) for i in range(6)]
        self.rodar(issue(" ".join(map(str, ps))))
        texto, kw = self.enviados[0]
        self.assertEqual(4, len(kw["arquivos"]))
        self.assertIn("veja na issue", texto)

    def test_um_arquivo_vai_solto(self):
        self.rodar(issue(str(self.arq("a.pdf"))))
        self.assertEqual(["envios/entregas/KRN-900/a.pdf"], self.enviados[0][1]["arquivos"])

    def test_so_imagens_viram_um_mosaico(self):
        from PIL import Image
        ps = []
        for i, cor in enumerate(("red", "green", "blue", "black", "white", "gray")):
            p = self.saida / f"p{i}.png"
            Image.new("RGB", (200, 100), cor).save(p)
            ps.append(p)
        self.rodar(issue(" ".join(map(str, ps))))
        rels = self.enviados[0][1]["arquivos"]
        self.assertEqual(["envios/entregas/KRN-900/KRN-900-previas.jpg"], rels)
        with Image.open(self.bot / rels[0]) as im:
            self.assertEqual((3 * 480, 2 * 480), im.size)

    def test_video_grande_nao_vai_e_zip_nunca(self):
        p = self.arq("reel.mp4", tam=2000)
        with mock.patch.object(ez, "LIMITE_VIDEO", 1000):
            self.rodar(issue(f"{p}"))
        texto, kw = self.enviados[0]
        self.assertEqual([], kw["arquivos"])
        self.assertIn("reel.mp4", texto)

    def test_in_review_nao_vai(self):
        i = issue(str(self.arq("a.pdf")))
        i["state"] = {"name": "In Review"}
        self.rodar(i)
        self.assertEqual([], self.enviados)

    def test_issue_reaberta_depois_de_done_nao_manda_pronta(self):
        i = issue(str(self.arq("a.pdf")))
        i["history"] = {"nodes": [{"fromState": {"type": "started"}, "toState": {"type": "completed"}},
                                  {"fromState": {"type": "completed"}, "toState": {"type": "started"}},
                                  {"fromState": {"type": "started"}, "toState": {"type": "completed"}}]}
        r = self.rodar(i)
        self.assertEqual([], self.enviados)
        self.assertEqual([], r["enviados"])

    def test_reabertura_na_segunda_pagina_do_historico(self):
        i = issue(str(self.arq("a.pdf")))
        i["id"] = "x1"
        i["history"] = {"nodes": [{"fromState": {"type": "started"}, "toState": {"type": "completed"}}],
                        "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}
        pag = {"issue": {"history": {"nodes": [{"fromState": {"type": "completed"}, "toState": {"type": "started"}}],
                                     "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
        with mock.patch.object(ez.linear, "gql", return_value=pag) as g:
            r = self.rodar(i)
        self.assertEqual("c1", g.call_args.kwargs["a"])
        self.assertEqual([], self.enviados)
        self.assertEqual([], r["enviados"])

    def test_primeira_conclusao_com_historico_vai(self):
        i = issue(str(self.arq("a.pdf")))
        i["history"] = {"nodes": [{"fromState": {"type": "started"}, "toState": {"type": "completed"}}]}
        self.rodar(i)
        self.assertEqual(1, len(self.enviados))

    def test_gigante_fica_so_no_linear(self):
        p = self.arq("grande.mp4", tam=3000)
        with mock.patch.object(ez, "LIMITE_ARQUIVO", 2000), mock.patch.object(ez, "LIMITE_VIDEO", 2000):
            self.rodar(issue(f"{p} e {self.arq('ok.pdf')}"))
        texto, kw = self.enviados[0]
        self.assertEqual(["envios/entregas/KRN-900/ok.pdf"], kw["arquivos"])
        self.assertIn("grande.mp4", texto)

    def test_titulo_curto(self):
        self.assertLessEqual(len(ez.titulo_curto("x" * 200)), 60)


class Vigia(Base):
    def test_cadencia_e_erro_isolado(self):
        p = self.arq("a.pdf")
        nos = [issue("sem nada", ident="KRN-1"), issue(f"{p}", ident="KRN-2")]
        consulta = lambda q, **v: {"issues": {"nodes": nos}}
        with mock.patch.object(ez, "admin_numero", return_value=None), mock.patch.object(ez, "_hosts_dos_sites", return_value=set()):
            f = ez.vigiar(consulta, str(self.dados), AGORA, log=lambda *_: None, avisar_fn=self.avisar, caixa=str(self.caixa))
            f2 = ez.vigiar(consulta, str(self.dados), AGORA + dt.timedelta(minutes=1), log=lambda *_: None,
                           avisar_fn=self.avisar, caixa=str(self.caixa))
        self.assertEqual(["KRN-2"], [x["issue"] for x in f])
        self.assertEqual([], f2)  # dentro dos 5 min
        self.assertIsNone(self.enviados[0][1]["para"])  # sem número: o bot manda ao dono

    def test_segue_a_paginacao(self):
        p1, p2 = self.arq("a.pdf"), self.arq("b.pdf", conteudo=b"outro")
        paginas = {None: {"nodes": [issue(f"{p1}", ident="KRN-1")], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}},
                   "c1": {"nodes": [issue(f"{p2}", ident="KRN-2")], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        consulta = lambda q, d=None, after=None: {"issues": paginas[after]}
        with mock.patch.object(ez, "admin_numero", return_value=None), mock.patch.object(ez, "_hosts_dos_sites", return_value=set()):
            f = ez.vigiar(consulta, str(self.dados), AGORA, log=lambda *_: None, avisar_fn=self.avisar, caixa=str(self.caixa))
        self.assertEqual(["KRN-1", "KRN-2"], [x["issue"] for x in f])


if __name__ == "__main__":
    unittest.main()


def test_o_que_passa_de_4_arquivos_e_nomeado_no_aviso(tmp_path):
    arqs = []
    for i in range(7):
        a = tmp_path / f"f{i}.pdf"
        a.write_bytes(bytes([i]) * 600)
        arqs.append(a)
    texto, saida = ez.montar("KRN-1", "t", arqs, [], tmp_path / "z")
    assert not any(a.suffix == ".zip" for a in saida)
    assert len(saida) == ez.MAX_ARQUIVOS and "veja na issue" in texto
    assert all(a.name in texto for a in arqs if a not in saida)  # o que ficou de fora é nomeado no aviso


def test_url_direta_de_arquivo_fora_do_linear_vai_como_link(tmp_path):
    c = ez.candidatos(issue("Veja https://cdn.exemplo.com/a/proposta.pdf e https://github.com/x/y/raw/main/a.png "
                            "e https://uploads.linear.app/z/b.png"), str(tmp_path))
    assert [(x["tipo"], x["origem"]) for x in c] == [("link", "https://cdn.exemplo.com/a/proposta.pdf")]


def test_arquivo_sobrescrito_depois_do_anexar_nao_vale(tmp_path):
    import hashlib
    a = tmp_path / "x.png"
    a.write_bytes(b"novo")
    rec = tmp_path / "claude" / "anexos"
    rec.mkdir(parents=True)
    (rec / "KRN-1.jsonl").write_text(json.dumps({"arquivo": str(a), "sha": hashlib.sha256(b"velho").hexdigest()}) + "\n")
    assert ez.candidatos({"identifier": "KRN-1"}, tmp_path) == []


class JanelaTest(unittest.TestCase):
    def test_janela_pela_data_de_conclusao(self):
        # (16/10): concluída na véspera e só comentada hoje foi ao zap; a janela é a da conclusão
        from hub import entrega_zap
        self.assertIn("completedAt: {gt: $d}", entrega_zap.Q_ISSUES)
        self.assertNotIn("updatedAt: {gt", entrega_zap.Q_ISSUES)
