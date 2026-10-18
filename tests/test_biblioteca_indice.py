"""Índice híbrido, injeção no despacho, prova de uso e arquivo da biblioteca. Sem rede: o embedding é um saco de
palavras com vetor aleatório fixo por palavra (a mesma palavra dá o mesmo vetor, então texto parecido fica perto)."""
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from hub import biblioteca as B, biblioteca_gabarito as GAB, biblioteca_indice as I, biblioteca_uso as U

VIDEO = """# Vídeo de lançamento com Remotion

- O que é: **kinetic type** e motion para vídeo de lançamento de produto.

## Reprodução nossa

```tsx
<Sequence from={0} durationInFrames={90}><Titulo/></Sequence>
```

## Conteúdo original

Texto literal sobre remotion, cortes na batida e legenda para quem assiste sem som.

## Onde aplicar
- Reels do ClienteJ e vídeo da asite01.
"""
EMAIL = """# ClienteN

Licença: MIT

O que é: e-mail open source com API compatível com a Resend, envia por SES.

## Onde aplicar
- Troca da Resend na asite01.
"""
SEO = """# Checklist de SEO técnico

Schema, canonical e sitemap em 20 itens.

## Onde aplicar
- fsite05 e ClienteI.
"""
GENERICA = """# Ferramenta genérica de ossium

Catálogo self-host sem projeto nomeado.
"""


def embed_falso(textos):
    out = []
    for t in textos:
        v = np.zeros(I.DIM, dtype=np.float32)
        for p in I.tokens(t):
            v += np.random.default_rng(int(hashlib.md5(p.encode()).hexdigest()[:8], 16)).standard_normal(I.DIM).astype(np.float32)
        out.append((v / (np.linalg.norm(v) or 1)).tolist())
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.d = t / "biblioteca"
        for rel, txt in {"motion/video.md": VIDEO, "ferramentas/clienten.md": EMAIL, "seo/checklist.md": SEO,
                         "ferramentas/ossium.md": GENERICA}.items():
            p = self.d / "receitas" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(txt, encoding="utf-8")
        self.db = t / "indice.db"
        self.uso = t / "uso.db"
        self.env = mock.patch.dict(os.environ, {"BIBLIOTECA_DIR": str(self.d), "BIBLIOTECA_INDICE": str(self.db),
                                                "BIBLIOTECA_USO": str(self.uso)})
        self.env.start()
        B._carregar.cache_clear()
        I._indice.cache_clear()

    def tearDown(self):
        self.env.stop()
        I._indice.cache_clear()
        self.tmp.cleanup()

    def indexar(self):
        return I.atualizar(embed=embed_falso, log=lambda *_: None)


class PedacosTest(unittest.TestCase):
    def test_secoes_com_tipo(self):
        p = I.pedacos(VIDEO)
        self.assertEqual([s for s, _ in p], ["o_que_e", "reproducao", "conteudo", "onde_aplicar"])
        self.assertIn("Sequence", p[1][1])
        self.assertTrue(p[3][1].startswith("Onde aplicar"))

    def test_hash_de_codigo_nao_vira_secao_e_corpo_longo_quebra(self):
        t = "# T\n\nabertura\n\n## Reprodução\n\n```bash\n# comentário que não é título\nls\n```\n"
        self.assertEqual([s for s, _ in I.pedacos(t)], ["o_que_e", "reproducao"])
        longo = "# T\n\n" + "\n\n".join(["parágrafo " * 60] * 10)
        self.assertGreater(len(I.pedacos(longo)), 1)
        self.assertTrue(all(len(x) <= I.PEDACO + 20 for _, x in I.pedacos(longo)))

    def test_conteudo_original_tem_teto(self):
        t = "# T\n\nabre\n\n" + "".join(f"## Parte {i}\n\ncorpo {i}\n\n" for i in range(30))
        self.assertEqual(sum(s == "conteudo" for s, _ in I.pedacos(t)), I.MAX_CONTEUDO)

    def test_tokens_sem_acento_stopword_e_plural(self):
        self.assertEqual(I.tokens("Vídeos de lançamento para os Reels"), ["video", "lancamento", "reel"])


class IndiceTest(Base):
    def test_atualizar_so_reindexa_o_que_mudou(self):
        r = self.indexar()
        self.assertEqual((r["receitas"], r["reindexadas"], r["sem_vetor"]), (4, 4, 0))
        self.assertGreater(r["vetores"], 4)
        chamadas = []
        r = I.atualizar(embed=lambda t: chamadas.append(t) or embed_falso(t), log=lambda *_: None)
        self.assertEqual((r["reindexadas"], r["vetores"], chamadas), (0, 0, []))
        (self.d / "receitas" / "seo" / "checklist.md").write_text(SEO + "\n- Cliente G.\n", encoding="utf-8")
        B._carregar.cache_clear()
        r = I.atualizar(embed=lambda t: chamadas.append(t) or embed_falso(t), log=lambda *_: None)
        self.assertEqual((r["reindexadas"], r["removidas"]), (1, 0))
        self.assertTrue(all("Checklist" in t for lote in chamadas for t in lote))
        (self.d / "receitas" / "seo" / "checklist.md").unlink()
        B._carregar.cache_clear()
        self.assertEqual(I.atualizar(embed=embed_falso, log=lambda *_: None)["removidas"], 1)
        self.assertNotIn("receitas/seo/checklist.md", [x["caminho"] for x in I.buscar("canonical sitemap", embed=embed_falso)])

    def test_embedding_fora_do_ar_deixa_sem_vetor_e_a_proxima_volta_completa(self):
        def cai(_):
            raise RuntimeError("sem OPENROUTER_API_KEY")
        avisos = []
        r = I.atualizar(embed=cai, log=avisos.append)
        self.assertEqual((r["vetores"], r["sem_vetor"] > 0, len(avisos)), (0, True, 1))
        self.assertEqual(I.buscar("remotion", embed=cai)[0]["caminho"], "receitas/motion/video.md")  # BM25 sozinho
        self.assertEqual(self.indexar()["sem_vetor"], 0)

    def test_busca_hibrida_palavra_exata_e_sentido(self):
        self.indexar()
        self.assertEqual(I.buscar("remotion", embed=embed_falso)[0]["caminho"], "receitas/motion/video.md")
        top = I.buscar("vídeo de lançamento do produto", embed=embed_falso)[0]
        self.assertEqual((top["caminho"], top["tema"], top["projetos"]), ("receitas/motion/video.md", "motion", ["asite01", "clientej", "reels"]))
        self.assertIn("ClienteJ", top["onde_aplicar"])
        self.assertEqual(I.buscar("e-mail resend", embed=embed_falso)[0]["licenca"], "MIT")

    def test_filtro_de_tema_e_projeto(self):
        self.indexar()
        q = "projeto catalogo self-host resend remotion checklist"
        self.assertEqual({s["tema"] for s in I.buscar(q, tema="seo", limite=10, embed=embed_falso)}, {"seo"})
        so_asite01 = {s["caminho"] for s in I.buscar(q, projeto="asite01", limite=10, embed=embed_falso)}
        self.assertIn("receitas/ferramentas/clienten.md", so_asite01)
        self.assertIn("receitas/ferramentas/ossium.md", so_asite01)  # sem projeto nomeado = vale para qualquer um
        self.assertNotIn("receitas/seo/checklist.md", so_asite01)
        estrito = {s["caminho"] for s in I.buscar(q, projeto="asite01", limite=10, embed=embed_falso, genericas=False)}
        self.assertNotIn("receitas/ferramentas/ossium.md", estrito)

    def test_limiar_melhor_nada_do_que_receita_errada(self):
        self.indexar()
        with mock.patch.object(I, "LIMIAR_COS", 0.2):
            ok = I.relevantes("vídeo de lançamento do produto com remotion", embed=embed_falso)
            self.assertEqual(ok[0]["caminho"], "receitas/motion/video.md")
            self.assertLessEqual(len(ok), I.LIMITE_DESPACHO)
        with mock.patch.object(I, "LIMIAR_COS", 0.99):
            self.assertEqual(I.relevantes("vídeo de lançamento do produto", embed=embed_falso), [])
        def cai(_):
            raise RuntimeError("fora")
        self.assertEqual(I.relevantes("vídeo de lançamento do produto", embed=cai), [])  # sem vetor da consulta: nada

    def test_consulta_da_issue_tira_url_preambulo_e_label_de_ruido(self):
        q = I.consulta_da_issue("Reel do ClienteJ", "Execute com a skill superpowers:x antes\nFazer o vídeo https://x.com/a/1 de lançamento",
                                ["merge", "codex", "plano-x", "skill:y", "design"])
        self.assertIn("Reel do ClienteJ", q)
        self.assertIn("vídeo", q)
        self.assertNotIn("http", q)
        self.assertNotIn("skill", q)
        self.assertTrue(q.endswith("design"))
        self.assertLessEqual(len(I.consulta_da_issue("t", "x" * 5000)), I.CORTE_DESCRICAO + 10)

    def test_bloco_do_prompt(self):
        self.indexar()
        achados = I.buscar("vídeo de lançamento remotion", embed=embed_falso)[:1]
        b = I.bloco_prompt(achados)
        self.assertTrue(b.startswith("## Receitas da biblioteca que se aplicam"))
        self.assertIn("`Receita: receitas/<tema>/<arquivo>.md`", b)
        self.assertIn("### Vídeo de lançamento com Remotion", b)
        self.assertIn("Onde aplicar:\n- Reels do ClienteJ", b)
        self.assertIn("<Sequence", b)
        self.assertNotIn("Reprodução nossa", b)  # o cabeçalho da seção não repete
        self.assertEqual(I.bloco_prompt([]), "")
        achados[0]["status"] = "curta"
        self.assertIn("ao usar pela 1ª vez, complete a extração", I.bloco_prompt(achados))

    def test_buscar_tela_cai_na_busca_por_texto_sem_indice(self):
        self.assertEqual([x["caminho"] for x in I.buscar_tela("remotion")], ["receitas/motion/video.md"])
        self.indexar()
        r = I.buscar_tela("vídeo de lançamento", limite=3)
        self.assertEqual(r[0]["caminho"], "receitas/motion/video.md")
        self.assertEqual(set(r[0]), {"caminho", "tema", "titulo", "status", "projetos", "data", "resumo", "trecho"})
        self.assertEqual(len(I.buscar_tela("", limite=10)), 4)  # sem termo: a lista de sempre


class UsoTest(Base):
    def item(self, ident="KRN-1", titulo="Vídeo de lançamento do produto", descricao="Fazer o vídeo de lançamento com Remotion"):
        return {"tipo": "issue", "identifier": ident, "titulo": titulo, "descricao": descricao, "labels": ["design"]}

    def test_bloco_da_issue_so_com_indice_e_reaproveita_sem_chamar_de_novo(self):
        self.assertEqual(U.bloco_da_issue(self.item()), "")  # sem índice: nada, e não falha
        self.indexar()
        chamadas = []
        emb = lambda t: chamadas.append(1) or embed_falso(t)
        with mock.patch.object(I, "LIMIAR_COS", 0.2):
            b = U.bloco_da_issue(self.item(), embed=emb)
            self.assertIn("Vídeo de lançamento com Remotion", b)
            self.assertEqual(U.bloco_da_issue(self.item(), embed=emb), b)
            self.assertEqual(len(chamadas), 1)  # a 2ª vez (outro tick) saiu do registro
            outra = U.bloco_da_issue(self.item(descricao="Outra coisa de remotion e lançamento"), embed=emb)
            self.assertEqual(len(chamadas), 2)
            self.assertIn("Remotion", outra)
        self.assertEqual(U.bloco_da_issue({**self.item(), "tipo": "update"}), "")
        rel = U.relatorio(r=None)
        self.assertEqual((rel["issues_com_receita"], rel["issues_consultadas"]), (1, 1))
        video = next(x for x in rel["receitas"] if x["caminho"] == "receitas/motion/video.md")
        self.assertEqual(video["injetada_em"], ["KRN-1"])

    def test_reindexar_invalida_o_bloco_guardado(self):
        self.indexar()
        chamadas = []
        emb = lambda t: chamadas.append(1) or embed_falso(t)
        with mock.patch.object(I, "LIMIAR_COS", 0.2):
            U.bloco_da_issue(self.item(), embed=emb)
            U.bloco_da_issue(self.item(), embed=emb)
            self.assertEqual(len(chamadas), 1)
            p = I.caminho_db()
            os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 10**9))  # o índice mudou
            U.bloco_da_issue(self.item(), embed=emb)
            self.assertEqual(len(chamadas), 2)

    def test_bloco_da_issue_nunca_derruba_o_despacho(self):
        self.indexar()
        def cai(_):
            raise OSError("rede")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(U.bloco_da_issue(self.item(), embed=cai), "")
        self.assertEqual(U.relatorio()["issues_consultadas"], 0)  # falha não grava: tenta de novo no próximo tick

    def test_receitas_citadas_no_corpo_do_pr(self):
        corpo = ("Fixes KRN-9\n\nReceita: receitas/motion/video.md\n- Receita: receitas/sites/gsap.md, receitas/3d/x.md\n"
                 "Receitas: receitas/motion/video.md\nveja receitas/seo/fora.md no texto\n> Receita: `receitas/a/b-c_d.md`")
        self.assertEqual(U.receitas_citadas(corpo), ["receitas/motion/video.md", "receitas/sites/gsap.md", "receitas/3d/x.md",
                                                     "receitas/a/b-c_d.md"])
        self.assertEqual(U.receitas_citadas(None), [])

    def pr(self, n, corpo, data="2026-10-16T10:00:00Z", repo="example-org/filmes"):
        return {"url": f"https://github.com/{repo}/pull/{n}", "repository": {"nameWithOwner": repo}, "title": f"PR {n}",
                "body": corpo, "createdAt": data}

    def test_coletar_grava_uso_e_relatorio_mostra_sem_uso(self):
        prs = {"example-org": [self.pr(1, "Fixes KRN-5\nReceita: receitas/motion/video.md"), self.pr(2, "sem citação")],
               "jpexemplo": [self.pr(3, "Receita: receitas/ferramentas/clienten.md\nKRN-7", "2026-08-01T00:00:00Z", "jpexemplo/s")]}
        gh = lambda args: prs[args[args.index("--owner") + 1]]
        self.assertEqual(U.coletar(gh=gh), 2)
        self.assertEqual(U.coletar(gh=gh), 2)  # idempotente
        agora = dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc)
        rel = U.relatorio(30, agora)
        video = next(x for x in rel["receitas"] if x["caminho"] == "receitas/motion/video.md")
        self.assertEqual(video["usada_em"][0]["issue"], "KRN-5")
        self.assertEqual(rel["prs_com_receita"], 2)
        self.assertNotIn("receitas/motion/video.md", rel["sem_uso"])
        self.assertIn("receitas/ferramentas/clienten.md", rel["sem_uso"])  # usada em agosto: fora dos 30 dias
        self.assertEqual(len(rel["sem_uso"]), 3)

    def test_coletar_com_gh_fora_do_ar_nao_levanta(self):
        def cai(_):
            raise RuntimeError("rate limit")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(U.coletar(gh=cai), 0)

    def test_balanco_de_7_dias(self):
        self.indexar()
        with mock.patch.object(I, "LIMIAR_COS", 0.2):
            U.bloco_da_issue(self.item("KRN-1"), embed=embed_falso)
            U.bloco_da_issue(self.item("KRN-2", "Outra coisa", "sem relação nenhuma xyzzy"), embed=embed_falso)
        hoje = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        U.coletar(gh=lambda a: [self.pr(1, "Fixes KRN-1\nReceita: receitas/motion/video.md", hoje)] if "example-org" in a else [])
        b = U.balanco(7)
        self.assertEqual((b["issues_despachadas"], b["com_receita"], b["usaram"]), (2, 1, 1))

    def test_tela_de_uso(self):
        from hub import biblioteca_tela
        U.coletar(gh=lambda a: [self.pr(1, "Fixes KRN-5\nReceita: receitas/motion/video.md", "2099-01-01T00:00:00Z")] if "example-org" in a else [])
        h = biblioteca_tela.uso()
        self.assertIn("Receitas aplicadas", h)
        self.assertIn("Vídeo de lançamento com Remotion", h)
        self.assertIn("KRN-5", h)
        self.assertIn("pull/1", h)
        self.assertIn("Sem uso em 30 dias", h)
        self.assertIn("ClienteN", h)  # nunca usada
        self.assertIn("vista=uso", biblioteca_tela.tela(vista="uso"))

    def test_issue_do_prompt_leva_o_bloco_e_sem_indice_fica_igual(self):
        from hub import linear
        item = {"tipo": "issue", "identifier": "KRN-3", "titulo": "Vídeo de lançamento do produto", "descricao": "d",
                "comentarios": [], "url": "u", "modelo": "sonnet", "repos": [], "estados": {}, "labels": []}
        self.assertNotIn("Receitas da biblioteca", linear.tarefa(item).prompt)
        self.indexar()
        with mock.patch.object(I, "LIMIAR_COS", 0.0), mock.patch.object(I, "embeddar", lambda t, timeout=None: embed_falso(t)):
            p = linear.tarefa({**item, "identifier": "KRN-4"}).prompt
        self.assertTrue(p.rstrip().split("## Receitas da biblioteca que se aplicam")[1])
        self.assertGreater(p.index("## Receitas da biblioteca"), p.index("## Como trabalhar"))  # no fim do prompt


class ArquivoTest(Base):
    def setUp(self):
        super().setUp()
        self.agora = dt.datetime(2026, 12, 20, tzinfo=dt.timezone.utc)
        (self.d / "receitas" / "INDEX-ferramentas.md").write_text(
            "| tema | título | arquivo | status |\n|---|---|---|---|\n| ferramentas | Ossium | [ferramentas/ossium.md](ferramentas/ossium.md) | completo |\n"
            "| ferramentas | ClienteN | [ferramentas/clienten.md](ferramentas/clienten.md) | completo |\n", encoding="utf-8")
        for x in B.receitas():  # data de criação velha (sem git no teste: injeta)
            pass
        self.datas = mock.patch.object(B, "_datas", lambda r: {f"receitas/{k}": "2026-10-01" for k in
                                                                  ("ferramentas/ossium.md", "ferramentas/clienten.md", "motion/video.md", "seo/checklist.md")})
        self.datas.start()
        B._carregar.cache_clear()

    def tearDown(self):
        self.datas.stop()
        B._carregar.cache_clear()
        super().tearDown()

    def medicao(self, desde):
        with U._conn() as c:
            c.execute("insert or replace into consulta values('KRN-0','h',?, '')", (desde,))

    def test_sem_60_dias_de_medicao_nao_arquiva_nada(self):
        self.medicao("2026-12-01T00:00:00Z")
        self.assertEqual(B.arquivar(agora=self.agora), [])

    def test_so_sem_projeto_e_sem_uso(self):
        self.medicao("2026-10-01T00:00:00Z")
        self.assertEqual(B.arquivar(agora=self.agora), ["receitas/ferramentas/ossium.md"])  # as outras têm projeto
        with U._conn() as c:  # usada há 10 dias: fica
            c.execute("insert into uso values('receitas/ferramentas/ossium.md','pr','r','KRN-1','t','2026-12-10')")
        self.assertEqual(B.arquivar(agora=self.agora), [])

    def test_aplicar_move_sem_apagar_e_tira_do_indice(self):
        self.medicao("2026-10-01T00:00:00Z")
        self.assertEqual(B.arquivar(agora=self.agora, aplicar=True), ["receitas/ferramentas/ossium.md"])
        self.assertTrue((self.d / "arquivo/receitas/ferramentas/ossium.md").is_file())
        self.assertFalse((self.d / "receitas/ferramentas/ossium.md").exists())
        self.assertNotIn("ossium", (self.d / "receitas/INDEX-ferramentas.md").read_text(encoding="utf-8"))
        self.assertIn("ClienteN", (self.d / "receitas/INDEX-ferramentas.md").read_text(encoding="utf-8"))
        self.assertIn("ossium.md", (self.d / "arquivo/INDEX.md").read_text(encoding="utf-8"))
        B._carregar.cache_clear()
        self.assertEqual(len(B.receitas()), 3)  # arquivo/ não é receita para busca nenhuma
        self.assertEqual(B.arquivar(agora=self.agora, aplicar=True), [])


class GabaritoTest(unittest.TestCase):
    # test_arquivo_bem_formado mora em tests/test_biblioteca_gabarito_real.py
    #: lê tests/biblioteca_gabarito.json, gabarito feito das nossas issues; fora do snapshot público.

    def test_avaliar_conta_precisao_recall_e_falso_disparo(self):
        casos = [{"issue": "A", "titulo": "t", "descricao": "", "labels": [], "projeto": None, "esperado": ["video"]},
                 {"issue": "B", "titulo": "t", "descricao": "", "labels": [], "projeto": None, "esperado": []}]
        r = {"A": [{"caminho": "receitas/motion/video.md", "cos": 0.6}, {"caminho": "receitas/seo/checklist.md", "cos": 0.5}],
             "B": [{"caminho": "receitas/seo/checklist.md", "cos": 0.45}]}
        with tempfile.TemporaryDirectory() as t, mock.patch.dict(os.environ, {"BIBLIOTECA_DIR": t}):
            for rel in ("motion/video.md", "seo/checklist.md"):
                (Path(t) / "receitas" / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(t) / "receitas" / rel).write_text("# x\n", encoding="utf-8")
            B._carregar.cache_clear()
            m = GAB.avaliar(casos, buscar=lambda c: r[c["issue"]])
            self.assertEqual((m["precisao"], m["recall"], m["falsos_disparos"], m["devolvidas"]), (1 / 3, 1.0, 1, 3))
            m = GAB.avaliar(casos, limiar=0.55, buscar=lambda c: r[c["issue"]])
            self.assertEqual((m["precisao"], m["falsos_disparos"]), (1.0, 0))
            B._carregar.cache_clear()


if __name__ == "__main__":
    unittest.main()


    def test_arquivar_de_novo_nao_sobrescreve_o_que_ja_foi_arquivado(self):
        self.medicao("2026-10-01T00:00:00Z")
        rel = "receitas/ferramentas/ossium.md"
        B.arquivar(agora=self.agora, aplicar=True)
        antigo = (self.d / "arquivo" / rel).read_text(encoding="utf-8")
        (self.d / rel).write_text("# Ossium de novo\n\nOutra versão.\n\n## Onde aplicar\n- nada\n", encoding="utf-8")
        B._carregar.cache_clear()
        B.arquivar(agora=self.agora, aplicar=True)
        self.assertEqual((self.d / "arquivo" / rel).read_text(encoding="utf-8"), antigo)
        self.assertIn("Ossium de novo", (self.d / "arquivo/receitas/ferramentas/ossium-2.md").read_text(encoding="utf-8"))
