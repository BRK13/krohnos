import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from hub import dataforseo, ranking

SITE = {"id": "x", "dominio": "exemplo.com.br", "hosts_alternativos": ["www.exemplo.com.br"], "ativo": True,
        "ranking": {"palavras": ["um", "dois", "tres"], "local": 2076, "idioma": "pt", "dispositivo": "mobile",
                    "profundidade": 40}}
AGORA = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def tarefa(*itens, status=20000):
    return [{"status_code": status, "result": [{"items": list(itens)}]}]


def org(dominio, pos, url="https://x/"):
    return {"type": "organic", "domain": dominio, "rank_group": pos, "url": url}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"HUB_DATA": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)

    def fatos(self):
        c = sqlite3.connect(Path(self.tmp.name) / "bi/metricas.db")
        return set(c.execute("select dia, negocio, fonte, metrica, dim, valor from fato"))


class MedirTest(Base):
    def test_posicao_por_dominio(self):
        respostas = {"um": tarefa({"type": "paid", "domain": "exemplo.com.br", "rank_group": 1}, org("outro-com.example", 2),
                                  org("www.exemplo.com.br", 3, "https://www.exemplo.com.br/a")),
                     "dois": tarefa(org("exemplo.com.br", 5)), "tres": tarefa(org("outro-com.example", 1))}
        chamadas = []

        def post(caminho, tarefas):
            chamadas.append((caminho, tarefas))
            return respostas[tarefas[0]["keyword"]]

        r = ranking.medir(SITE, AGORA, post)
        self.assertEqual(chamadas[0][0], "serp/google/organic/live/advanced")
        self.assertEqual(chamadas[0][1], [{"keyword": "um", "location_code": 2076, "language_code": "pt",
                                           "device": "mobile", "depth": 40}])
        self.assertEqual([(i["palavra"], i["posicao"]) for i in r["itens"]], [("um", 3), ("dois", 5), ("tres", None)])
        self.assertEqual(r["itens"][0]["url"], "https://www.exemplo.com.br/a")
        self.assertIsNone(r["itens"][2]["url"])
        self.assertEqual((r["fonte"], r["dia"]), ("dataforseo", "2026-10-11"))
        self.assertEqual(json.loads((Path(self.tmp.name) / "ranking/x/2026-10-11.json").read_text()), r)
        self.assertEqual(self.fatos(), {
            ("2026-10-11", "x", "ranking", "medida", p, 1.0) for p in ("um", "dois", "tres")}
            | {("2026-10-11", "x", "ranking", "posicao", "um", 3.0), ("2026-10-11", "x", "ranking", "posicao", "dois", 5.0)})

    def test_dia_e_de_brasilia(self):
        r = ranking.medir(SITE, datetime(2026, 10, 11, 1, 0, tzinfo=timezone.utc), lambda c, t: tarefa())
        self.assertEqual(r["dia"], "2026-10-10")

    def test_falha_dupla_vira_lacuna_e_falha_simples_passa(self):
        n = {"um": 0, "dois": 0, "tres": 0}

        def post(caminho, tarefas):
            k = tarefas[0]["keyword"]
            n[k] += 1
            if k == "um":
                raise RuntimeError("caiu")
            if k == "dois":
                return tarefa(org("exemplo.com.br", 4)) if n[k] > 1 else tarefa(status=50000)
            return tarefa()

        r = ranking.medir(SITE, AGORA, post)
        por = {i["palavra"]: i for i in r["itens"]}
        self.assertEqual((n["um"], n["dois"]), (2, 2))
        self.assertIn("caiu", por["um"]["erro"])
        self.assertEqual(por["dois"]["posicao"], 4)
        self.assertNotIn("erro", por["dois"])
        self.assertFalse({f for f in self.fatos() if f[4] == "um"})


class SobreposicaoTest(Base):
    def gravar_config(self, **cfg):
        arq = ranking.arquivo_config("x")
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text(json.dumps({"palavras": ["novo"], "local": 1, "idioma": "en", "dispositivo": "desktop",
                                   "profundidade": 10, **cfg}))

    def test_config_cadastro_ou_arquivo(self):
        self.assertEqual(ranking.config(SITE), {**SITE["ranking"], "fixadas": []})
        self.assertIsNone(ranking.config({**SITE, "ranking": None}))
        self.gravar_config()
        self.assertEqual(ranking.config(SITE)["palavras"], ["novo"])
        self.assertEqual(ranking.config(SITE)["fixadas"], [])

    def test_medir_usa_sobreposicao(self):
        self.gravar_config()
        chamadas = []

        def post(caminho, tarefas):
            chamadas.append(tarefas)
            return tarefa(org("exemplo.com.br", 2))

        r = ranking.medir(SITE, AGORA, post)
        self.assertEqual(chamadas, [[{"keyword": "novo", "location_code": 1, "language_code": "en",
                                      "device": "desktop", "depth": 10}]])
        self.assertEqual((r["local"], [i["palavra"] for i in r["itens"]]), (1, ["novo"]))

    def test_medir_com_pasta_nao_toca_o_bi(self):
        pasta = Path(self.tmp.name) / "mcp/ranking/x/medicoes"
        r = ranking.medir(SITE, AGORA, lambda c, t: tarefa(org("exemplo.com.br", 4)), pasta=pasta)
        self.assertEqual(json.loads((pasta / "2026-10-11.json").read_text()), r)
        self.assertFalse((Path(self.tmp.name) / "bi/metricas.db").exists())
        self.assertFalse((Path(self.tmp.name) / "ranking/x").exists())
        ranking.medir(SITE, AGORA, lambda c, t: 1 / 0, pasta=pasta)  # falha total mantém a medida da pasta
        self.assertEqual(json.loads((pasta / "2026-10-11.json").read_text()), r)

    def test_ler_junta_pastas_e_fixadas_primeiro(self):
        ranking.medir(SITE, AGORA, lambda c, t: tarefa(org("exemplo.com.br", 4)))
        pasta = Path(self.tmp.name) / "mcp/ranking/x/medicoes"
        ranking.medir(SITE, datetime(2026, 10, 18, 12, tzinfo=timezone.utc), lambda c, t: tarefa(org("exemplo.com.br", 2)), pasta=pasta)
        self.gravar_config(palavras=["um", "dois", "tres"], fixadas=["tres"])
        r = ranking.ler("x")
        self.assertEqual(r["medidoEm"], "2026-10-18")
        self.assertEqual([(p["palavra"], p["fixada"]) for p in r["palavras"]],
                         [("tres", True), ("um", False), ("dois", False)])
        self.assertEqual([h["dia"] for h in r["palavras"][0]["historico"]], ["2026-10-11", "2026-10-18"])


class MainTest(Base):
    def medir(self, *ativos):
        medidos = []
        doc = lambda s: (medidos.append(s["id"]), {"itens": [{"palavra": "a", "posicao": 1, "url": None}]})[1]
        with mock.patch.object(ranking.sites, "ativos", return_value=list(ativos)), mock.patch.object(ranking, "medir", doc), \
             mock.patch("sys.stdout"), mock.patch("sys.stderr") as err:
            return ranking.main(["medir"]), medidos, "".join(c.args[0] for c in err.write.call_args_list)

    def test_config_quebrado_nao_para_os_outros(self):
        arq = ranking.arquivo_config("x")
        arq.parent.mkdir(parents=True)
        arq.write_text("{quebrado")
        codigo, medidos, err = self.medir(SITE, {**SITE, "id": "y"})
        self.assertEqual((codigo, medidos), (1, ["y"]))
        self.assertIn("x: config do ranking ilegível", err)

    def test_mede_site_so_com_sobreposicao(self):
        SobreposicaoTest.gravar_config(self)
        self.assertEqual(self.medir({**SITE, "ranking": None})[:2], (0, ["x"]))


class FalhaTotalTest(Base):
    def test_falha_total_no_mesmo_dia_preserva_medida(self):
        def cai(c, t):
            raise RuntimeError("api fora")

        ranking.medir(SITE, AGORA, lambda c, t: tarefa(org("exemplo.com.br", 4)))
        antes = self.fatos()
        r = ranking.medir(SITE, AGORA, cai)
        self.assertFalse(any("erro" in i for i in r["itens"]))  # mantém as medidas boas do dia
        self.assertEqual(self.fatos(), antes)
        lido = ranking.ler("x")
        self.assertEqual([(p["palavra"], p["posicao"]) for p in lido["palavras"]], [("um", 4), ("dois", 4), ("tres", 4)])

    def test_falha_total_sem_arquivo_nao_grava(self):
        ranking.medir(SITE, AGORA, lambda c, t: 1 / 0)
        self.assertFalse((Path(self.tmp.name) / "ranking/x").exists())
        self.assertFalse(ranking.ler("x")["ok"])


def fixture_openseo(caminho):
    c = sqlite3.connect(caminho)
    c.executescript("""
        create table rank_tracking_configs(id text, domain text, location_code int, serp_depth int, devices text);
        create table rank_check_runs(id text, config_id text);
        create table rank_snapshots(run_id text, keyword text, device text, position int, url text, checked_at text);
        insert into rank_tracking_configs values ('c1', 'exemplo.com.br', 2076, 40, 'mobile'), ('c2', 'outro-org.example', 2076, 40, 'mobile');
        insert into rank_check_runs values ('r1', 'c1'), ('r2', 'c1'), ('r3', 'c2');
        insert into rank_snapshots values
          ('r1', 'um', 'mobile', 7, 'https://exemplo.com.br/', '2026-10-06 16:59:00'),
          ('r1', 'dois', 'mobile', null, null, '2026-10-06 16:59:00'),
          ('r2', 'um', 'mobile', 5, 'https://exemplo.com.br/b', '2026-10-08 01:00:00'),
          ('r3', 'um', 'mobile', 1, 'https://outro-org.example/', '2026-10-06 16:59:00');""")
    c.commit()
    c.close()


class ImportarTest(Base):
    def test_importa_converte_dia_e_nao_duplica(self):
        db = Path(self.tmp.name) / "o.sqlite"
        fixture_openseo(db)
        self.assertEqual(ranking.importar_openseo(db, [SITE]), 5)
        self.assertEqual(ranking.importar_openseo(db, [SITE]), 5)
        self.assertEqual(self.fatos(), {
            ("2026-10-06", "x", "ranking", "medida", "um", 1.0), ("2026-10-06", "x", "ranking", "posicao", "um", 7.0),
            ("2026-10-06", "x", "ranking", "medida", "dois", 1.0),
            ("2026-10-07", "x", "ranking", "medida", "um", 1.0), ("2026-10-07", "x", "ranking", "posicao", "um", 5.0)})
        d = json.loads((Path(self.tmp.name) / "ranking/x/2026-10-07.json").read_text())
        self.assertEqual((d["fonte"], d["local"], d["dispositivo"], d["profundidade"]), ("openseo", 2076, "mobile", 40))
        self.assertEqual(d["itens"], [{"palavra": "um", "posicao": 5, "url": "https://exemplo.com.br/b"}])
        self.assertFalse((Path(self.tmp.name) / "ranking/x/2026-10-08.json").exists())

    def test_nao_sobrescreve_medida_propria(self):
        db = Path(self.tmp.name) / "o.sqlite"
        fixture_openseo(db)
        ranking.medir(SITE, datetime(2026, 10, 6, 15, tzinfo=timezone.utc), lambda c, t: tarefa(org("exemplo.com.br", 2)))
        ranking.importar_openseo(db, [SITE])
        d = json.loads((Path(self.tmp.name) / "ranking/x/2026-10-06.json").read_text())
        self.assertEqual(d["fonte"], "dataforseo")


class LerTest(Base):
    def escrever(self, dia, itens):
        p = Path(self.tmp.name) / "ranking/x"
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{dia}.json").write_text(json.dumps({"site": "x", "dia": dia, "fonte": "dataforseo", "local": 2076,
                                                   "dispositivo": "mobile", "profundidade": 40, "itens": itens}))

    def test_variacao_e_historico(self):
        self.escrever("2026-09-01", [{"palavra": "um", "posicao": 20, "url": None}])
        self.escrever("2026-10-04", [{"palavra": "um", "posicao": 9, "url": None}, {"palavra": "dois", "posicao": 3, "url": None}])
        self.escrever("2026-10-11", [{"palavra": "um", "posicao": 6, "url": "u"}, {"palavra": "dois", "posicao": None, "url": None},
                                     {"palavra": "tres", "posicao": 2, "url": None}])
        r = ranking.ler("x", semanas=4)
        self.assertEqual((r["ok"], r["medidoEm"], r["fonte"], r["local"]), (True, "2026-10-11", "dataforseo", 2076))
        por = {p["palavra"]: p for p in r["palavras"]}
        self.assertEqual((por["um"]["posicao"], por["um"]["anterior"], por["um"]["variacao"]), (6, 9, 3))
        self.assertEqual(por["um"]["historico"], [{"dia": "2026-10-04", "posicao": 9}, {"dia": "2026-10-11", "posicao": 6}])
        self.assertIsNone(por["dois"]["variacao"])
        self.assertIsNone(por["tres"]["anterior"])

    def test_sem_medicao(self):
        r = ranking.ler("x")
        self.assertEqual((r["ok"], r["reason"]), (False, "sem_medicao"))
        self.assertIn("python3 -m hub.ranking medir --site x", r["comoResolver"])


class DataForSeoTest(unittest.TestCase):
    def test_chave_nunca_vai_na_excecao(self):
        k = "SEGREDOabc123"
        with mock.patch.dict(os.environ, {"DATAFORSEO_AUTH": k}):
            casos = [urllib.error.HTTPError("u", 401, f"bad {k}", {}, None), urllib.error.URLError(f"falha {k}"),
                     OSError(k)]
            for exc in casos:
                with mock.patch.object(urllib.request, "urlopen", side_effect=exc):
                    with self.assertRaises(RuntimeError) as c:
                        dataforseo.post("x", [{}])
                self.assertNotIn(k, str(c.exception))
            resp = mock.MagicMock()
            resp.__enter__.return_value = resp
            resp.read.return_value = json.dumps({"status_code": 40100, "status_message": f"chave {k} ruim"}).encode()
            with mock.patch.object(urllib.request, "urlopen", return_value=resp):
                with self.assertRaises(RuntimeError) as c:
                    dataforseo.post("x", [{}])
            self.assertIn("40100", str(c.exception))
            self.assertNotIn(k, str(c.exception))
