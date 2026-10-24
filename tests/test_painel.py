import datetime as dt
import json
import os
import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from hub import painel, ui, usuarios
from tests import sessao_teste


_ENV = None


def setUpModule():
    """Sem `gh auth token` real e sem memo de credencial vazando entre testes."""
    global _ENV
    from unittest import mock
    from hub import github_api
    _ENV = mock.patch.dict("os.environ", {"GH_TOKEN": "token-de-teste"})
    _ENV.start()
    github_api._AUTH.clear()


def tearDownModule():
    from hub import github_api
    _ENV.stop()
    github_api._AUTH.clear()


def cockpit_neutro(teste):
    """Troca as 4 funções de hub.cockpit por dados neutros: teste de página nunca vai ao Linear nem ao disco das contas."""
    from hub import cockpit
    painel._CK.clear()
    teste.addCleanup(painel._CK.clear)
    for nome, v in (("agentes", []), ("fila", []), ("linear_resumo", {}), ("pendencias", {})):
        p = mock.patch.object(cockpit, nome, lambda *a, v=v, **k: v)
        p.start()
        teste.addCleanup(p.stop)


class PainelTest(unittest.TestCase):
    def setUp(self):
        cockpit_neutro(self)
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "propostas").mkdir()
        (d / "propostas" / "20261008-061126-clientei-conteudo-192d.json").write_text(
            json.dumps({"id": "20261008-061126-clientei-conteudo-192d", "resumo": "<script>x</script>",
                        "estado": "pendente"}), encoding="utf-8")
        (d / "claude").mkdir()
        (d / "claude" / "linear-app-token.json").write_text('{"token": "SEGREDO"}', encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"HUB_DATA": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_github_mostra_amostra_e_cobertura_sem_consultar_api(self):
        with mock.patch("hub.github_api.resumo", return_value=[("<script>", "http", 304, 7)]):
            status, corpo = painel.rota("/github")
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", corpo)
        self.assertIn("local-ci das VPS", corpo)
        self.assertIn("<th>total</th>", corpo)
        self.assertIn("304", corpo)

    def test_github_banco_ilegivel_nao_derruba_a_tela(self):
        import sqlite3
        for erro in (sqlite3.OperationalError("x"), PermissionError("root")):
            with mock.patch("hub.github_api.resumo", side_effect=erro):
                status, corpo = painel.rota("/github")
            self.assertEqual(status, 200)
            self.assertIn("Sem amostra", corpo)

    def test_github_cota_e_pivo(self):
        cotas = [("abc123def456aaaa", "App", 4000, 5000, 9000.0)]
        linhas = [("pequeno", "http", 200, 1), ("grande", "gh", 0, 5)]
        with mock.patch("hub.github_api.resumo", return_value=linhas), mock.patch("hub.github_api.cotas", return_value=cotas):
            status, corpo = painel.rota("/github")
        self.assertIn("App abc123", corpo)
        self.assertIn("4000", corpo)
        self.assertLess(corpo.index("grande"), corpo.index("pequeno"))

    def test_cookie_e_volta(self):  # era test_senha (HTTP Basic): o login agora é cookie de sessão
        self.assertEqual(painel.token_do("a=1; __Host-krohnos_sessao=abc_-1; b=2"), "abc_-1")
        self.assertEqual(painel.token_do('x="sem fim; __Host-krohnos_sessao=t1'), "t1")
        for ruim in (None, "", "__Host-krohnos_sessao=", "outro=__Host-krohnos_sessao", ";;;=", "__Host-krohnos_sessao"):
            self.assertEqual(painel.token_do(ruim), "", ruim)
        self.assertEqual(painel.volta_ok("/uso?aba=x"), "/uso?aba=x")
        for ruim in (None, "", "uso", "//mal.test", "/\\mal.test", "https://mal.test", "/a\r\nX: 1", "/a b"):
            self.assertEqual(painel.volta_ok(ruim), "/", ruim)

    def test_ip_so_de_proxy_confiavel(self):
        # cliente da internet forja o XFF: vale o IP da conexão (IP global; TEST-NET conta como privado no ipaddress)
        self.assertEqual(painel.ip_cliente("8.8.4.4", "1.2.3.4"), "8.8.4.4")
        # Traefik na rede do docker (privada): vale o último item
        self.assertEqual(painel.ip_cliente("172.19.0.1", "10.9.9.9, 203.0.113.7"), "203.0.113.7")
        self.assertEqual(painel.ip_cliente("127.0.0.1", "203.0.113.8"), "203.0.113.8")
        # item que não é IP, ou XFF vazio: o da conexão
        self.assertEqual(painel.ip_cliente("172.19.0.1", "lixo"), "172.19.0.1")
        self.assertEqual(painel.ip_cliente("172.19.0.1", None), "172.19.0.1")
        # painel.proxies definida substitui o padrão privado
        with mock.patch.dict(os.environ, {"KROHNOS_PAINEL_PROXIES": '["8.8.4.0/24"]'}):
            self.assertEqual(painel.ip_cliente("8.8.4.4", "203.0.113.7"), "203.0.113.7")
            self.assertEqual(painel.ip_cliente("172.19.0.1", "203.0.113.7"), "172.19.0.1")

    def test_redteam_com_modulo_200_e_sem_modulo_404(self):
        """hub.redteam fica fora do snapshot público; sem ele a rota /redteam responde 404."""
        with mock.patch.object(painel, "redteam_tela", mock.Mock(corpo=lambda: "<p>RT</p>")):
            status, corpo = painel.rota("/redteam")
            self.assertEqual(status, 200)
            self.assertIn("RT", corpo)
        with mock.patch.object(painel, "redteam_tela", None):
            self.assertEqual(painel.rota("/redteam")[0], 404)

    def test_rotas_escapam_e_nao_saem_da_pasta(self):
        status, corpo = painel.rota("/proposta/20261008-061126-clientei-conteudo-192d")
        self.assertEqual(status, 200)
        self.assertNotIn("<script>x", corpo)
        for c in ("/proposta/..%2Fclaude%2Flinear-app-token", "/proposta/../claude/linear-app-token",
                  "/site/../claude", "/claude/linear-app-token.json", "/site/nao-existe"):
            status, corpo = painel.rota(c)
            self.assertEqual(status, 404, c)
            self.assertNotIn("SEGREDO", corpo)
        for c in ("/operacao", "/relatorio", "/propostas", "/uso?aba=execucoes", "/monitor?aba=sistema", "/monitor?aba=deploys", "/site/clientei", "/uso", "/site/fsite06",
                  "/propostas?estado=<script>"):
            self.assertEqual(painel.rota(c)[0], 200, c)
            self.assertNotIn("SEGREDO", painel.rota(c)[1])

    def test_componentes_visuais_saem_do_ui(self):  # os componentes antigos do painel foram para hub/ui.py (tests/test_ui.py)
        for velho in ("sparkline", "kpi", "medidor", "selo", "card", "tabela", "PONTE_CSS"):
            self.assertFalse(hasattr(painel, velho), velho)

    @unittest.skipUnless(painel.avaliacoes_tela, "tela de avaliações (evals) é do privado: fora do snapshot público não existe")
    def test_avaliacoes_rota_e_menu(self):
        st, h = painel._rota("/avaliacoes")[:2]
        self.assertEqual(st, 200)
        self.assertIn("Avaliações", h)
        self.assertIn("Nenhuma rodada ainda.", h)
        self.assertIn("/avaliacoes", [c for _, _, itens in ui.menu() for c, _ in itens])

    def test_pagina_tem_menu_e_tema(self):
        h = painel.pagina("T", "")
        for x in ("/monitor", "/pendencias", "/agentes", "/linear", "/propostas", "Geist", '<nav class="lado"', "Krohnos"):
            self.assertIn(x, h)

    def _fixture_site(self):
        """3 snapshots de 7 dias (fim em 5, 6 e 7/10), sobrepostos: o snapshot k grava 10*(k+1) cliques por dia.
        O snapshot 0 ainda traz 22 a 28/9 (janela anterior)."""
        d = Path(self.tmp.name)
        for k in range(3):
            snap = d / "snapshots" / f"2026-10-0{5 + k}"
            snap.mkdir(parents=True)
            fim = dt.date(2026, 10, 5 + k)
            dias = [fim - dt.timedelta(n) for n in range(7)]
            if k == 0:
                dias += [dt.date(2026, 9, x) for x in range(22, 29)] + [dt.date(2026, 9, 1)]
            rows = [{"keys": [x.isoformat()], "clicks": 10 * (k + 1), "impressions": 100, "ctr": .1,
                     "position": 8.0 - k} for x in dias]
            rows.append({"keys": ["lixo"], "clicks": 5, "impressions": 1, "position": 1})  # data inválida: ignorada
            (snap / "fsite06-gsc.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
            (snap / "fsite06-ga4.json").write_text(json.dumps({"rows": [
                {"dimensionValues": [{"value": "google"}], "metricValues": [{"value": "50"}, {"value": "30"}, {"value": "5"}]}]}),
                encoding="utf-8")
        (d / "gsc" / "fsite06").mkdir(parents=True)
        (d / "gsc" / "fsite06" / "latest.json").write_text(json.dumps({"oportunidades": [
            {"tipo": "vazamento", "busca": "quiropraxia <b>", "pagina": "https://site-b.exemplo.com/", "impressoes": 2705,
             "cliques": 44, "posicao": 5.4, "ctr": .0163, "ctr_esperado": .0505, "cliques_perdidos": 93, "janela": None},
            {"tipo": "reforcar", "busca": "massagem", "pagina": "https://site-b.exemplo.com/a", "paginas_que_aparecem": [],
             "impressoes": 402, "cliques": 0, "posicao": 10.5, "janela": None},
            {"tipo": "buraco", "busca": "fsite06 sul", "pagina": None, "paginas_que_aparecem": ["https://site-b.exemplo.com/b"],
             "impressoes": 90, "cliques": 0, "posicao": 15.0, "janela": None}]}), encoding="utf-8")

    def test_serie_sem_somar_duas_vezes_e_ponderada(self):
        self._fixture_site()
        pts = dict(painel.serie("fsite06", "cliques"))
        self.assertEqual(pts["2026-10-05"], 30)  # está nos 3 snapshots: vale o mais novo
        self.assertEqual(pts["2026-10-07"], 30)
        self.assertEqual(pts["2026-09-29"], 10)  # só no primeiro
        self.assertEqual(len(pts), 16)  # 22/9 a 7/10; 1/9 fica fora dos 28 dias
        self.assertAlmostEqual(painel.total("fsite06", "ctr"), painel.total("fsite06", "cliques") / painel.total("fsite06", "impressoes"))
        self.assertAlmostEqual(dict(painel.serie("fsite06", "posicao"))["2026-10-07"], 6.0)
        self.assertEqual(len(painel.serie("fsite06", "sessoes")), 3)  # sem GA4 por data: um ponto por snapshot
        self.assertIsNone(painel.delta("fsite06", "sessoes"))
        self.assertIsNone(painel.total("fsite06", "sessoes"))  # totais de 7 dias sobrepostos não somam

    def test_delta_7_contra_7(self):
        self._fixture_site()
        pts = dict(painel.serie("fsite06", "cliques"))
        ult = sum(pts[f"2026-10-0{x}"] for x in range(1, 8))
        ant = sum(pts[f"2026-09-{x}"] for x in range(24, 31))
        self.assertGreater(ult, 0)
        self.assertAlmostEqual(painel.delta("fsite06", "cliques"), (ult - ant) / ant * 100)
        self.assertIsNone(painel.delta("nao-existe", "cliques"))
        self.assertLess(painel.delta("fsite06", "posicao"), 0)  # posição caiu (melhorou)

    def test_pagina_do_site_e_visao_geral(self):
        self._fixture_site()
        status, h = painel.rota("/site/fsite06")
        self.assertEqual(status, 200)
        for x in ("O que fazer agora", "selo alerta", "selo ok", "selo ruim", "<svg", 'aria-label="Site"',
                  "reescrever título/descrição", "subir a página", "criar página", 'table class="ord"',
                  ">Vazamento<", ">Reforçar<", ">Buraco<", 'data-v="2705"', '<th class="n">Impressões', "Cliques · 16 dias"):
            self.assertIn(x, h)
        self.assertNotIn("Cliques · 28 dias", h)  # a série só cobre 16 dias
        self.assertNotIn(">reforcar<", h)
        self.assertNotIn("quiropraxia <b>", h)  # escapado
        sc = h[h.index("O que fazer agora"):h.index("Respostas de IA")]
        self.assertNotIn("md-src", sc)
        self.assertIn("ainda sem janela", h)
        self.assertEqual(h.count('<a href="/site/fsite06"'), 1)
        status, h = painel.rota("/operacao")
        self.assertEqual(status, 200)
        from hub import sites
        for s in sites.carregar():
            self.assertIn(f'href="/site/{s["id"]}"', h)
        d = Path(self.tmp.name) / "propostas"
        for pid, est in (("20261008-061126-fsite06-conteudo-192d", "pendente"), ("20261008-061127-fsite06-seo-1111", "enviada"),
                         ("20261008-061128-fsite06-seo-2222", "aplicada"), ("20261008-061129-fisiofoo-seo-3333", "pendente")):
            (d / f"{pid}.json").write_text(json.dumps({"id": pid, "estado": est, "resumo": "r"}), encoding="utf-8")
        h = painel.rota("/operacao")[1]
        linha = re.search(r'<tr><td[^>]*><a href="/site/fsite06">.*?</tr>', h, re.S).group(0)
        self.assertRegex(linha, r'<td class="n" data-v="2" data-rotulo="[^"]+">2</td>')  # só pendente/enviada com token exato
        self.assertEqual(painel.rota("/site/asite01")[0], 200)  # sem snapshot nem JSON: não quebra

    def test_relatorio_tabela_com_selo_por_fonte(self):
        (Path(self.tmp.name) / "latest-report.md").write_text(
            "# Nimbus — coleta 2026-10-14\n\nPeríodos móveis.\n\n| Projeto | Google | Bing |\n|---|---|---|\n"
            "| asite01 | OK | OK |\n| clientee | sem acesso/dado | OK <b> |\n| fsite07 | sem acesso/dado | falta token |\n"
            "\nasite01: IA (4 sessões)\n", encoding="utf-8")
        status, h = painel.rota("/relatorio")
        self.assertEqual(status, 200)
        self.assertIn('<span class="selo ok">OK</span>', h)
        self.assertIn('<span class="selo alerta">sem acesso/dado</span>', h)
        self.assertIn("de 4 · 50", h)
        self.assertIn("Coleta de 14/10", h)
        self.assertIn("OK &lt;b&gt;", h)
        self.assertNotIn("OK <b>", h)
        self.assertIn("asite01: IA (4 sessões)", h)
        self.assertNotIn("fsite07", h)  # ativo:false no cadastro: fora da tabela e dos KPIs
        (Path(self.tmp.name) / "latest-report.md").write_text("# só texto\n\nsem tabela\n", encoding="utf-8")
        self.assertIn("md-src", painel.rota("/relatorio")[1])
        (Path(self.tmp.name) / "latest-report.md").unlink()
        self.assertIn("Nenhuma coleta gravada", painel.rota("/relatorio")[1])

    def test_visao_geral_sem_janelas_abertas_mas_site_tem(self):
        self._fixture_site()
        self.assertNotIn("Janelas abertas", painel.rota("/operacao")[1])
        self.assertIn("Mudanças e janelas", painel.rota("/site/fsite06")[1])

    def test_o_que_fazer_agora_nao_esconde_vazamento(self):
        self._fixture_site()
        arq = Path(self.tmp.name) / "gsc" / "fsite06" / "latest.json"
        o = json.loads(arq.read_text())
        o["oportunidades"] += [{"tipo": "reforcar", "busca": f"ref{i}", "pagina": "https://site-b.exemplo.com/r",
                                "impressoes": 150 + 50 * i, "cliques": 0, "posicao": 11.0} for i in range(6)]
        arq.write_text(json.dumps(o), encoding="utf-8")
        h = painel.rota("/site/fsite06")[1]
        sc = h[h.index("O que fazer agora"):h.index("Vazamentos:")]
        self.assertEqual(sc.count('<article class="item"'), 5)
        for x in ("quiropraxia", "fsite06 sul", "posição 5,4"):  # um de cada tipo; o vazamento tem mais impressões
            self.assertIn(x, sc)
        self.assertLess(sc.index("quiropraxia"), sc.index("massagem"))

    def test_vps_claude_propostas_sem_json_cru(self):
        d = Path(self.tmp.name)
        (d / "vps").mkdir()
        (d / "vps" / "saude.json").write_text(json.dumps({
            "em": "2026-10-08T22:40:01+00:00", "disco": {"pct": 89, "livre_gb": 9}, "memoria": {"pct": 37, "swap_mb": 3305},
            "zumbis": {"total": 4, "por_pai": {"crond (pid 1)": 3}}, "reboot": {"pendente": True, "pacotes": ["linux-base"]},
            "atualizacoes": {"total": 11, "seguranca": 11}, "containers": {"caidos": []},
            "vigia": {"asite01-wpp": {"conectado": False, "min_sem_sinal": 30}},
            "backups": [{"nome": "banco <x>", "ok": False, "idade_h": None}],
            "certificados": [{"host": "a-com.example", "dias": 5}, {"host": "b-com.example"}],
            "deploys": [{"app": "radar", "status": "done", "em": "2026-10-06T18:55"}, {"app": "novo", "em": "2026-10-08T19:55"}],
            "problemas": ["disco em 89%"], "coisa_nova": {"a": 1}}), encoding="utf-8")
        (d / "consertos.jsonl").write_text(json.dumps({"em": "2026-10-08T22:09:05+00:00", "quebrou": "Disco <b>", "conserto": "limpar",
                                                         "estado": "precisa_fulano"}) + "\n", encoding="utf-8")
        h = painel.rota("/monitor?aba=sistema")[1] + painel.rota("/monitor?aba=deploys")[1]
        self.assertNotIn('"pct":', h)
        self.assertNotIn("{'", h)
        for x in ("med alerta", "Reboot pendente", "linux-base", "crond (pid 1)", "3.305", "coisa_nova", "disco em 89%",
                  "selo ruim", "a-com.example", "asite01-wpp", "Alvo"):
            self.assertIn(x, h)
        self.assertNotIn("banco <x>", h)
        self.assertNotIn("Disco <b>", h)
        self.assertIn("08/10 19:40", h)  # 22:40 UTC em Brasília
        self.assertNotIn("22:40", h)
        self.assertLess(h.index(">novo<"), h.index(">radar<"))  # deploy mais recente primeiro
        self.assertNotIn(">None<", h)
        (d / "claude" / "uso.jsonl").write_text(json.dumps({"inicio": "2026-10-08T10:00:00+00:00", "nome": "t", "ok": True,
                                                              "custo_usd": 1.5, "conta": "2"}) + "\n", encoding="utf-8")
        (d / "claude" / "pausa.json").write_text(json.dumps({"ate": "2020-10-09T03:00:00+00:00", "origem": "agente",
                                                               "mensagem": "limit <i>"}), encoding="utf-8")  # vencida
        (d / "claude" / "pausa-2.json").write_text(json.dumps({"ate": "2099-10-09T04:00:00+00:00"}), encoding="utf-8")
        self.assertEqual(painel.rota("/claude"), (302, "", {"Location": "/uso?aba=execucoes"}))
        h = painel.rota("/uso?aba=execucoes")[1]
        for x in ("Em pausa · conta 2", "até 09/10 01:00", "0 tokens", 'class="kpi"'):
            self.assertIn(x, h)
        self.assertNotIn("conta 1", h)
        self.assertNotIn("origem", h)
        self.assertNotIn('"ate"', h)
        self.assertNotIn("limit <i>", h)

    def test_execucoes_assinatura_sem_dolar_api_com_dolar(self):
        usos = [{"conta": "1", "nome": "assinatura", "tokens": {"input": 123}, "custo_usd": 74},
                {"conta": "api", "nome": "reserva", "custo_usd": 2}]
        with mock.patch.object(painel, "_jsonl", return_value=usos):
            h = painel._execucoes()
        self.assertIn("123 tokens", h)
        self.assertNotIn("74", h)
        self.assertIn("US$", h)
        with mock.patch.object(painel, "_jsonl", return_value=usos[:1]):
            self.assertNotIn("US$", painel._execucoes())

    def test_datas_em_brasilia(self):
        self.assertEqual(painel._quando("2026-10-09T13:05:00+00:00"), "2026-10-09 10:05")
        self.assertEqual(painel._quando("2026-10-09T13:05:00Z"), "2026-10-09 10:05")
        self.assertEqual(painel._quando("2026-10-06T18:55"), "2026-10-06 18:55")  # sem fuso: como veio
        self.assertEqual(painel._quando(None), "")
        d = Path(self.tmp.name) / "propostas"
        (d / "20261009-130500-x-1111.json").write_text(json.dumps({"id": "20261009-130500-x-1111", "estado": "feita",
                                                                    "criada_em": "2026-10-09T13:05:00+00:00"}), encoding="utf-8")
        h = painel.rota("/propostas")[1]
        self.assertIn("09/10 10:05", h)
        self.assertNotIn("UTC", h)

    def test_rota_execucao_404_para_ref_ruim_e_200_para_valida(self):
        from hub import rastreio
        rastreio.gravar("abcd1234abcd1234", {"n": 1, "tipo": "ferramenta", "ferramenta": "Bash", "entrada": {"c": "ls"},
                                              "saida": "<script>x</script>"})
        self.assertEqual(painel.rota("/execucao/..%2Fx")[0], 404)
        self.assertEqual(painel.rota("/execucao/zzzz")[0], 404)
        self.assertEqual(painel.rota("/execucao/ffff0000")[0], 404)
        status, h = painel.rota("/execucao/abcd1234?passo=1")
        self.assertEqual(status, 200)
        self.assertNotIn("<script>x</script>", h)
        self.assertEqual(painel.rota("/execucao")[0], 200)

    def test_propostas_filtro_por_estado(self):
        d = Path(self.tmp.name) / "propostas"
        (d / "20261008-061127-fsite06-seo-1111.json").write_text(json.dumps({"id": "x", "estado": "feita", "resumo": "ja feita"}),
                                                                 encoding="utf-8")
        h = painel.rota("/propostas?estado=pendente")[1]
        self.assertIn("&lt;script&gt;x", h)
        self.assertNotIn("ja feita", h)
        self.assertIn('href="/propostas?estado=feita"', h)
        for q in ("?estado=<script>", "?estado=nada", "?estado="):
            h = painel.rota("/propostas" + q)[1]
            self.assertIn("ja feita", h)
            self.assertNotIn("<script>alert", h)
        self.assertEqual(painel.rota("/propostas?estado=pendente")[0], 200)
        self.assertEqual(painel.rota("/nada?estado=pendente")[0], 404)

    def test_propostas_paginadas_em_50(self):
        d = Path(self.tmp.name) / "propostas"
        for k in range(120):
            pid = f"20261101-{k:06d}-x-1111"
            (d / f"{pid}.json").write_text(json.dumps({"id": pid, "estado": "feita", "resumo": f"r{k:03d}x"}), encoding="utf-8")
        h = painel.rota("/propostas")[1]
        self.assertIn("Página 1 de 3", h)
        self.assertEqual(h.count('class="resumo-2"'), 50)
        self.assertIn('href="/propostas?pagina=2"', h)
        self.assertNotIn("← Anterior", h)
        h = painel.rota("/propostas?pagina=3")[1]
        self.assertIn("Página 3 de 3", h)
        self.assertIn('href="/propostas?pagina=2"', h)
        self.assertNotIn("Próxima →", h)
        self.assertLessEqual(h.count('class="resumo-2"'), 50)
        h = painel.rota("/propostas?estado=feita&pagina=2")[1]
        self.assertIn('href="/propostas?estado=feita"', h)  # voltar à 1 não leva ?pagina=1
        self.assertIn('href="/propostas?estado=feita&amp;pagina=3"', h)
        for q in ("?pagina=999", "?pagina=0", "?pagina=-1", "?pagina=abc"):
            self.assertEqual(painel.rota("/propostas" + q)[0], 200, q)
        self.assertEqual(painel.rota("/propostas?estado=pendente&pagina=2")[0], 200)


class CockpitTelasTest(unittest.TestCase):
    AG = [{"conta": n, "tarefa": t, "issue": i, "titulo": None, "desde": None, "minutos": m, "ultimos_passos": p,
           "cinco_horas": 40, "semana": 20} for n, t, i, m, p in
          [("1", None, None, None, []), ("2", "linear-krn-16", "KRN-16", 30, ["[Bash] npm test"]), ("3", None, None, None, [])]]
    LR = {"por_estado": {"Todo": 2}, "issues": [{"id": "x", "identifier": "KRN-16", "titulo": "Vitrine <b>nova</b>",
                                                 "estado": "In Progress", "projeto": "ClienteJ", "url": "https://l",
                                                 "labels": ["plano-x"], "atualizada": "2026-10-09T11:00:00Z"}],
          "planos": [{"label": "plano-x", "feitas": 1, "total": 3}], "esteira": {"estoque": 9, "despachadas_7d": 4}}
    PE = {"pedidos": [{"n": 33, "id": "a", "resumo": "retreinar", "tipo": "vps", "criada_em": "2026-10-09T10:00:00+00:00"}],
          "travadas": [], "perguntas": []}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HUB_DATA": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def rota(self, caminho):
        from hub import cockpit, painel
        with mock.patch.object(cockpit, "agentes", lambda *a, **k: self.AG), \
                mock.patch.object(cockpit, "linear_resumo", lambda *a, **k: self.LR), \
                mock.patch.object(cockpit, "pendencias", lambda *a, **k: self.PE), \
                mock.patch.object(cockpit, "fila", lambda *a, **k: []):
            return painel.rota(caminho)

    def test_agentes_mostra_as_3_contas_e_o_passo_atual(self):
        status, h = self.rota("/agentes")
        self.assertEqual(status, 200)
        self.assertIn("KRN-16", h)
        self.assertIn("npm test", h)
        self.assertEqual(h.count('class="conta conta-'), 3)
        self.assertIn('<div class="grade n3">', h)
        self.assertIn("Contas trabalhando", h)
        self.assertIn(">1 de 3<", h)

    def test_quatro_contas_com_a_faixa_ao_lado_do_nome(self):
        self.AG = [dict(a, faixa="construtor") for a in self.AG] + [
            {"conta": "4", "faixa": "operador", "tarefa": None, "ultimos_passos": [], "cinco_horas": None, "semana": None}]
        for caminho in ("/agentes", "/operacao", "/agora.fragmento"):
            status, h = self.rota(caminho)
            self.assertEqual(status, 200)
            self.assertEqual(h.count('class="conta conta-'), 4, caminho)
            self.assertIn('<div class="grade n4">', h)
            self.assertIn('Conta 1 <span class="faixa">· construtor</span>', h)
            self.assertIn('Conta 4 <span class="faixa">· operador</span>', h)

    def test_agentes_sem_estado_do_orq_avisa_e_nao_quebra(self):
        status, h = self.rota("/agentes")
        self.assertEqual(status, 200)
        self.assertIn("Sem estado do orquestrador ainda", h)
        self.assertEqual(h.count('class="conta conta-'), 3)

    def test_agentes_mostra_orquestrador_e_revisor_codex(self):
        d = Path(os.environ["HUB_DATA"])
        (d / "orch").mkdir()
        (d / "claude").mkdir()
        (d / "claude" / "uso-contas.json").write_text(json.dumps({"orquestrador": {
            "cinco_horas": {"pct": 37}, "semana": {"pct": 12}}}), encoding="utf-8")
        (d / "orch" / "estado.json").write_text(json.dumps({"em": "2026-10-10T15:00:00+00:00", "varredura": {
            "rodando": None, "checada_em": "2026-10-10T15:00:00+00:00",
            "ultima": {"inicio": "2026-10-10T14:30:00+00:00", "codigo": 0, "motivo": "pedidos aguardando/erro: 103",
                       "resumo": "- **Resolvido:** fechei o pedido <b>103</b>"}},
            "codex": {"login_ok": True, "modelo": "gpt-6.1-sol", "raciocinio": "high",
                      "hoje": {"revisados": 5, "com_achado": 2, "sem_achados": 3},
                      "ultimo": {"repo": "clienteh", "pr": 483, "url": "https://github.com/acme/clienteh/pull/483",
                                 "em": "2026-10-10T15:21:00+00:00", "achados": True}}}), encoding="utf-8")
        with mock.patch("hub.agenda._semana_executor", return_value=None):  # não lê as sessões reais do Codex
            status, h = self.rota("/agentes")
        self.assertEqual(status, 200)
        for x in ("Orquestrador", "Em espera", "pedidos aguardando/erro: 103", "Resolvido: fechei o pedido &lt;b&gt;103",
                  "37%", "Revisor Codex", "gpt-6.1-sol · raciocínio high", "Login ok",
                  "<b>5</b> PRs revisados hoje · 2 com achado · 3 sem achados",
                  'href="https://github.com/acme/clienteh/pull/483"', "clienteh#483", "com achado"):
            self.assertIn(x, h)
        self.assertNotIn("<b>103", h)
        self.assertNotIn("Semana", h)  # Claude não tem limite semanal na tela (10/10)

    def test_linear_escapa_titulo(self):
        status, h = self.rota("/linear")
        self.assertEqual(status, 200)
        self.assertIn("Vitrine &lt;b&gt;nova&lt;/b&gt;", h)
        for x in ('href="https://l"', "Em andamento", "Em revisão", "Travadas", '<details class="mais">', 'action="/acao/comentar"',
                  'name="csrf"', 'name="issue" value="KRN-16"'):
            self.assertIn(x, h)

    def test_uso_abas(self):
        for aba, tem in (("", "Janela de 5 h mais cheia"), ("?aba=contas", "Janela de 5 h mais cheia"),
                         ("?aba=execucoes", "Execuções recentes"), ("?aba=<x>", "Janela de 5 h mais cheia")):
            status, h = painel.rota("/uso" + aba)
            self.assertEqual(status, 200)
            self.assertIn(tem, h)
            self.assertIn('class="filtro abas', h)  # com 4 abas ganha "tem-sel" (select no celular)
            self.assertIn("Retomadas", h)  #
            self.assertNotIn("<x>", h)

    def test_pendencias_e_home(self):
        self.assertIn("retreinar", self.rota("/pendencias")[1])
        from hub import config
        self.assertIn(f"Pendências do {config.dono()}", self.rota("/pendencias")[1])  #
        status, h = self.rota("/operacao")
        self.assertEqual(status, 200)
        self.assertIn("Agora", h)
        self.assertIn("plano-x", h)
        # Pedir aos Claudes: botão no topo que abre o <dialog> com o formulário real (CSRF)
        self.assertIn('data-abre="pedir"', h)
        self.assertIn('<dialog id="pedir"', h)
        self.assertIn('action="/acao/criar-issue"', h)
        self.assertIn('name="csrf"', h)
        self.assertNotIn("Cota das contas", h)

    def test_fragmento_selo_e_link_so_https(self):
        self.LR = dict(self.LR, issues=[dict(self.LR["issues"][0], url="javascript:alert(1)")])
        status, h = self.rota("/agora.fragmento")
        self.assertEqual(status, 200)
        self.assertNotIn("<html", h)
        self.assertIn('href="/pendencias"', h)
        self.assertNotIn("javascript:", self.rota("/linear")[1])
        self.assertIn('class="acoes" data-id="a"', self.rota("/pendencias")[1])
        self.assertIn(">servidor<", self.rota("/pendencias")[1])
        self.assertIn("0/0", painel._progresso("plano-y", None, None))  # plano sem números não derruba a home
        self.assertRegex(self.rota("/monitor?aba=sistema")[1], r'Pendências</span><span class=n>1<')

    def test_pendencias_com_linear_fora_avisa_e_pedido_sem_numero_nao_tem_botao(self):
        from hub import cockpit
        pe = {"pedidos": [{"n": None, "id": "sem-n", "resumo": "ainda não enviado", "tipo": "vps", "criada_em": None}],
              "travadas": [], "perguntas": []}

        def cai(*a, **k):
            raise OSError("linear fora")
        with mock.patch.object(cockpit, "pendencias", lambda *a, **k: pe), mock.patch.object(cockpit, "linear_resumo", cai):
            painel._CK.clear()
            status, h = painel.rota("/pendencias")
        self.assertEqual(status, 200)
        self.assertIn("ainda não enviado", h)
        self.assertNotIn('action="/acao/pedido"', h)
        self.assertNotIn(">#", h)
        self.assertIn("Ainda não foi para o WhatsApp", h)
        self.assertEqual(h.count("Linear indisponível agora"), 2)  # travadas e perguntas: não "Nada esperando você"

    def test_home_so_promete_decidir_com_pedido_numerado(self):
        sem_n = {"pedidos": [dict(self.PE["pedidos"][0], n=None)], "travadas": [], "perguntas": []}
        self.assertIn("Ver e decidir →", self.rota("/agora.fragmento")[1])
        self.PE = sem_n
        h = self.rota("/agora.fragmento")[1]
        self.assertNotIn("decidir", h)
        self.assertIn("Ver →", h)

    def test_cockpit_fora_do_ar_nao_derruba(self):
        from hub import cockpit, painel

        chamadas = []

        def cai(*a, **k):
            chamadas.append(1)
            raise OSError("linear fora")
        with mock.patch.object(cockpit, "agentes", cai), mock.patch.object(cockpit, "linear_resumo", cai), \
                mock.patch.object(cockpit, "pendencias", cai), mock.patch.object(cockpit, "fila", cai):
            for c in ("/operacao", "/agentes", "/linear", "/pendencias", "/agora.fragmento"):
                status, h = painel.rota(c)
                self.assertEqual(status, 200, c)
                self.assertIn("indisponível agora", h, c)
                self.assertNotIn("linear fora", h, c)
            chamadas.clear()
            painel.rota("/operacao")
            self.assertEqual(len(chamadas), 3)  # agentes, linear_resumo, pendencias: uma vez cada, mesmo falhando

    def test_metas_tela_escapa_grafico_e_ancora(self):
        from hub import metas
        with mock.patch("hub.metas.abrir_issue", return_value="KRN-1 https://linear.app/x"):
            m = metas.criar("LCP <script>x</script>", site="fsite06", alvo=2000, unidade="ms",
                            medir={"tipo": "manual", "como": "a mão"})
        for v in (3000, 2800):
            metas.registrar(m["id"], v, "m")
        status, h = self.rota("/metas")
        self.assertEqual(status, 200)
        self.assertNotIn("<script>x", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertIn("<svg", h)
        self.assertIn(f'id={m["id"]}>', h)
        self.assertIn("rodada 2/50", h)
        self.assertIn('href="/metas#', self.rota("/pendencias")[1])  # faixa Nave

    def test_moedor_responde_com_o_contador(self):  # (F5 do moedor)
        from hub import moedor
        if not hasattr(painel, "moedor_tela") or painel.moedor_tela is None:
            self.skipTest("a tela do moedor não vai no snapshot público")
        moedor.contar("krohnos", "aceitas", 2)
        status, h = self.rota("/moedor")
        self.assertEqual(status, 200)
        self.assertIn('href="/moedor"', h)
        self.assertIn("krohnos", h)
        self.assertIn("Gasto", h)

    def test_confiabilidade_responde_com_os_cinco_numeros(self):  # (F2)
        status, h = self.rota("/confiabilidade")
        self.assertEqual(status, 200)
        self.assertIn('href="/confiabilidade"', h)
        from hub import telemetria
        telemetria.registrar(telemetria.de_uso({"inicio": "2026-10-17T12:00:00+00:00", "fim": "2026-10-17T12:01:00+00:00",
                                                "conta": "1", "nome": "linear-krn-3", "ok": True, "tipo": "primeira"}))
        h = self.rota("/confiabilidade")[1]
        for rotulo in ("Sucesso", "Issue→merge", "Custo por issue fechada", "Retrabalho", "Avisos falsos"):
            self.assertIn(rotulo, h)

    def test_metas_vazia(self):
        self.assertIn("Nenhuma meta ainda", self.rota("/metas")[1])

    def test_metas_sem_rodadas_no_json_e_pulada(self):
        d = Path(os.environ["HUB_DATA"]) / "claude" / "metas"
        d.mkdir(parents=True)
        (d / "m-1.json").write_text(json.dumps({"id": "m-1", "objetivo": "x", "estado": "ativa", "direcao": "menor"}), encoding="utf-8")
        self.assertEqual(self.rota("/metas")[0], 200)
        self.assertEqual(self.rota("/pendencias")[0], 200)

    def test_metas_json_corrompido_e_pulado(self):
        d = Path(os.environ["HUB_DATA"]) / "claude" / "metas"
        d.mkdir(parents=True)
        (d / "m-1.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.rota("/metas")[0], 200)

    def test_criar_meta_converte_segundos_e_grava(self):
        from hub import acoes, metas
        with mock.patch("hub.metas.abrir_issue", return_value="KRN-9"):
            ok, msg = acoes.executar("criar-meta", {"objetivo": "Home rápida", "url": "https://exemplo.com.br/",
                                                    "metrica": "lcp_ms", "alvo": "2,0"}, dt.datetime.now(dt.timezone.utc))
        self.assertTrue(ok, msg)
        self.assertIn("KRN-9", msg)
        self.assertEqual(metas.listar()[0]["alvo"], 2000)
        self.assertEqual(metas.listar()[0]["unidade"], "ms")  # unidade em branco com LCP vira ms
        self.assertFalse(acoes.executar("criar-meta", {"objetivo": "x"}, None)[0])


if __name__ == "__main__":
    unittest.main()


class CotaCodexNaHome(unittest.TestCase):
    def test_codex_aparece_na_cota_das_contas(self):
        with mock.patch.object(painel.C, "com_login", return_value=[]):
            html = painel.uso_linha(usos=[], leitura={}, codex={"pct": 8.0, "reset": "2026-10-14T15:53:14+00:00"})
        self.assertIn("Codex · semana", html)
        self.assertIn("8", html)

    def test_codex_sem_leitura_nao_quebra(self):
        with mock.patch.object(painel.C, "com_login", return_value=[]):
            html = painel.uso_linha(usos=[], leitura={}, codex={})
        self.assertIn("Codex · semana", html)
        self.assertIn("sem leitura", html)


class ComecarTest(unittest.TestCase):
    """Assistente /comecar (hub/comecar.py): só liga com krohnos.toml sem dono/marcador; servidor real em porta efêmera."""

    def setUp(self):
        from http.server import ThreadingHTTPServer
        import threading
        from hub import config
        cockpit_neutro(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        for p in ("dados", "seg"):
            (self.d / p).mkdir()
        self.toml = self.d / "krohnos.toml"
        self.env = mock.patch.dict(os.environ, {
            "KROHNOS_CONFIG": str(self.toml), "HUB_DATA": str(self.d / "dados"),
            "KROHNOS_CAMINHOS_SEGREDOS": str(self.d / "seg"), "KROHNOS_CAMINHOS_DADOS": str(self.d / "dados"),
            "PAINEL_SENHA": ""})
        self.env.start()
        config.recarregar()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), painel.Painel)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.cookie = None

    def tearDown(self):
        from hub import config
        self.srv.shutdown()
        self.srv.server_close()
        self.env.stop()
        config.recarregar()
        self.tmp.cleanup()

    def req(self, caminho, dados=None, auth=None, cookie=None, origin=True):
        """auth = cabeçalho Cookie da sessão do painel (antes, usuário:senha do Basic); junta com cookie."""
        import urllib.error
        import urllib.parse
        import urllib.request

        class Sem(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        h = {}
        if auth or cookie:
            h["Cookie"] = "; ".join(c for c in (cookie, auth) if c)
        if dados is not None and origin:
            h["Origin"] = self.base
        r = urllib.request.Request(self.base + caminho, data=urllib.parse.urlencode(dados).encode() if dados is not None else None, headers=h)
        try:
            resp = urllib.request.build_opener(Sem).open(r)
        except urllib.error.HTTPError as ex:
            resp = ex
        return resp.status, resp.read().decode(), resp.headers

    def tomlvazio(self, extra=""):
        self.toml.write_text('[produto]\nnome = "Krohnos"\n' + extra, encoding="utf-8")
        from hub import config
        config.recarregar()

    def passo(self, passo, dados, auth=None):
        st, corpo, h = self.req("/comecar?passo=" + passo, auth=auth)
        self.assertEqual(st, 200, corpo[:200])
        ck = h["Set-Cookie"].split(";")[0]
        token = ck.split("=", 1)[1]
        if passo == "dono" and "token_config" not in dados:
            dados = {**dados, "token_config": self.token_config()}
        return self.req("/comecar", {"csrf": token, "passo": passo, **dados}, auth=auth, cookie=ck)

    def token_config(self):
        from hub import comecar
        return comecar.token_config()

    def sessao_do(self, h):
        """Cookie da sessão que o POST /comecar (passo dono) já devolveu."""
        ck = h["Set-Cookie"]
        self.assertIn("__Host-krohnos_sessao=", ck)
        return ck.split(";")[0]

    def test_sem_toml_legado_nada_muda(self):
        self.assertFalse(self.toml.exists())
        st, _, h = self.req("/pendencias")  # era 401 (Basic): agora o login, nunca o assistente
        self.assertEqual((st, h["Location"]), (303, "/entrar?volta=%2Fpendencias"))
        self.assertEqual(self.req("/pendencias", auth="__Host-krohnos_sessao=x")[0], 303)
        self.assertEqual(self.req("/comecar")[0], 404)
        self.assertEqual(self.req("/saude")[:2], (200, "ok"))
        self.assertEqual(self.req("/pendencias", auth=sessao_teste.cookie())[0], 200)

    def test_toml_sem_dono_leva_ao_comecar(self):
        self.tomlvazio()
        st, _, h = self.req("/pendencias")
        self.assertEqual((st, h["Location"]), (303, "/comecar"))
        self.assertEqual(self.req("/saude")[0], 200)  # aberta
        st, corpo, _ = self.req("/comecar")
        self.assertEqual(st, 200)
        self.assertIn("Configuração pendente: use o token exibido na instalação", corpo)  # tela neutra, sem o formulário do dono
        self.assertNotIn("Passo 1 de 5", corpo)
        self.assertNotIn('name="senha"', corpo)
        self.assertEqual(self.req("/acao/criar-issue", {"x": "1"})[0], 303)  # POST de ação não executa

    def test_fluxo_completo_dono_e_depois_404(self):
        self.tomlvazio()
        st, _, h = self.passo("dono", {"nome": "Ana", "email": "ana@exemplo.test", "senha": "senha-forte-123", "senha2": "senha-forte-123"})
        self.assertEqual((st, h["Location"].startswith("/comecar?passo=github")), (303, True))
        a = self.sessao_do(h)  # o passo do dono já entra com ele
        self.assertEqual([(u["email"], u["papel"]) for u in usuarios.listar()], [("ana@exemplo.test", "dono")])
        self.assertFalse((self.d / "seg" / "painel.env").exists())  # antes: PAINEL_SENHA em painel.env
        self.assertIn('email = "ana@exemplo.test"', self.toml.read_text())
        # com dono: rotas pedem login (era 401 do Basic) e /comecar passa a pedir a sessão do dono
        self.assertEqual(self.req("/pendencias")[0], 303)
        self.assertEqual(self.req("/pendencias", auth=a)[0], 200)
        st, _, h = self.req("/comecar")
        self.assertEqual((st, h["Location"]), (303, "/entrar?volta=%2Fcomecar"))
        self.assertEqual(self.req("/comecar", auth=sessao_teste.cookie(papel="operador"))[0], 403)
        self.assertEqual(self.passo("github", {"org": "minha-org", "token": "ghp_" + "a" * 30}, auth=a)[0], 303)
        env = self.d / "seg" / "github.env"
        self.assertEqual(oct(env.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("a" * 30, self.toml.read_text())
        st, corpo, _ = self.req("/comecar?passo=github", auth=a)
        self.assertIn("termina em aaaa", corpo)
        self.assertNotIn("a" * 30, corpo)
        self.assertEqual(self.passo("linear", {"acao": "pular"}, auth=a)[0], 303)
        self.assertEqual(self.passo("projeto", {"dominio": "minhaempresa.test", "nome": "Minha Empresa"}, auth=a)[0], 303)
        proj = json.loads((self.d / "dados" / "projetos" / "minhaempresa.json").read_text())
        self.assertEqual(proj["dominio"], "minhaempresa.test")
        self.assertFalse((self.d / "dados" / "comecar.concluido").exists())
        st, _, h = self.passo("conta", {"conta": "1"}, auth=a)
        self.assertEqual((st, h["Location"].split("?")[0]), (303, "/"))
        self.assertTrue((self.d / "dados" / "comecar.concluido").exists())
        self.assertEqual(self.req("/comecar", auth=a)[0], 404)
        self.assertEqual(self.req("/pendencias", auth=a)[0], 200)
        self.assertEqual(self.req("/pendencias")[0], 303)  # era 401

    def test_dono_existente_no_toml_nao_redireciona(self):
        self.tomlvazio('[dono]\nemail = "ana@exemplo.test"\n')
        st, _, h = self.req("/pendencias")  # era 401 (Basic)
        self.assertEqual((st, h["Location"]), (303, "/entrar?volta=%2Fpendencias"))
        self.assertEqual(self.req("/pendencias", auth=sessao_teste.cookie())[0], 200)

    def test_csrf_e_origem(self):
        self.tomlvazio()
        dados = {"passo": "dono", "email": "ana@exemplo.test", "senha": "senha-forte-123", "senha2": "senha-forte-123",
                 "token_config": self.token_config()}
        self.assertEqual(self.req("/comecar", {**dados, "csrf": "abc"})[0], 403)  # sem cookie
        self.assertEqual(self.req("/comecar", {**dados, "csrf": "abc"}, cookie="krohnos_comecar=outro")[0], 403)
        self.assertEqual(self.req("/comecar", {**dados, "csrf": "abc"}, cookie="krohnos_comecar=abc", origin=False)[0], 303)
        self.assertEqual(self.toml.read_text().count("ana@exemplo.test"), 1)
        self.tomlvazio()
        usuarios.banco().unlink()  # volta a "nenhum usuário" (antes: apagava o painel.env)
        import urllib.request
        r = urllib.request.Request(self.base + "/comecar", data=b"csrf=abc", headers={"Origin": "https://outro.test", "Cookie": "krohnos_comecar=abc"})
        with self.assertRaises(Exception) as cm:
            urllib.request.urlopen(r)
        self.assertEqual(cm.exception.code, 403)
        self.assertEqual(self.req("/comecar", {"csrf": "abc", "passo": "dono"}, cookie="krohnos_comecar=abc")[0], 303)  # erro volta com aviso, nada gravado
        self.assertNotIn("ana@exemplo.test", self.toml.read_text())

    # ---- token de configuração do primeiro acesso (quem abre /comecar primeiro não vira dono) ----

    DONO = {"nome": "Ana", "email": "ana@exemplo.test", "senha": "senha-forte-123", "senha2": "senha-forte-123"}

    def post_dono(self, extra):
        return self.req("/comecar", {"csrf": "abc", "passo": "dono", **self.DONO, **extra}, cookie="krohnos_comecar=abc")

    def test_token_sem_token_nao_cria_dono(self):
        self.tomlvazio()
        from hub import comecar
        comecar._anunciado = False
        st, corpo, _ = self.req("/comecar")  # GET: tela neutra, 200 (nunca 404 nem erro), e o token só vai ao log
        self.assertEqual(st, 200)
        self.assertIn("Configuração pendente", corpo)
        real = self.token_config()
        self.assertNotIn(real, corpo)
        self.assertEqual(self.post_dono({})[0], 303)  # sem token_config: recusa (volta com aviso)
        self.assertEqual(self.post_dono({"token_config": ""})[0], 303)
        self.assertEqual(usuarios.contar(), 0)
        self.assertNotIn("ana@exemplo.test", self.toml.read_text())

    def test_token_errado_recusa_e_bloqueia(self):
        from hub import comecar
        self.tomlvazio()
        real = self.token_config()
        for _ in range(comecar.MAX_TENTATIVAS):
            st, _, h = self.post_dono({"token_config": "errado"})
            self.assertEqual(st, 303)
            self.assertIn("erro=", h["Location"])
        self.assertEqual(usuarios.contar(), 0)
        # bloqueado: nem o token certo vale até passar o prazo
        st, _, h = self.post_dono({"token_config": real})
        self.assertIn("Muitas", urllib.parse.unquote(h["Location"]))
        self.assertEqual(usuarios.contar(), 0)
        # a tela de token também responde 429, sem mostrar o passo do dono
        st, _, h = self.req("/comecar")
        ck = h["Set-Cookie"].split(";")[0]
        st, corpo, _ = self.req("/comecar", {"csrf": ck.split("=", 1)[1], "passo": "token", "token_config": real}, cookie=ck)
        self.assertEqual(st, 429)
        self.assertNotIn('name="senha"', corpo)
        # passado o bloqueio, o certo vale
        arq = self.d / "seg" / comecar.ARQ_TENTATIVAS
        arq.write_text('{"falhas": 5, "ate": 1}')
        self.assertEqual(self.post_dono({"token_config": real})[0], 303)
        self.assertEqual(usuarios.contar(), 1)

    def test_token_certo_cria_uma_vez_e_depois_ninguem(self):
        from hub import comecar
        self.tomlvazio()
        real = self.token_config()
        st, _, h = self.req("/comecar")
        ck = h["Set-Cookie"].split(";")[0]
        st, corpo, _ = self.req("/comecar", {"csrf": ck.split("=", 1)[1], "passo": "token", "token_config": real}, cookie=ck)
        self.assertEqual(st, 200)  # token certo: mostra o formulário do dono
        self.assertIn('name="senha"', corpo)
        st, _, h = self.post_dono({"token_config": real})
        self.assertEqual((st, h["Location"].startswith("/comecar?passo=github")), (303, True))
        self.assertEqual([u["email"] for u in usuarios.listar()], ["ana@exemplo.test"])
        self.assertFalse((self.d / "seg" / comecar.ARQ_TOKEN).exists())  # queimado
        # depois disso, ninguém mais: nem com o token antigo, nem sem sessão
        st, _, h = self.post_dono({"token_config": real, "email": "invasor@exemplo.test"})
        self.assertEqual(usuarios.contar(), 1)
        self.assertEqual(self.req("/comecar")[0:1], (303,))  # redireciona para o login
        st, _, h = self.req("/comecar")
        self.assertEqual(h["Location"], "/entrar?volta=%2Fcomecar")
        self.assertNotIn("invasor@exemplo.test", self.toml.read_text())

    def test_token_arquivo_0600_e_anuncio_no_log(self):
        import io
        from hub import comecar
        self.tomlvazio()
        buf = io.StringIO()
        v = comecar.anunciar(buf)
        self.assertEqual(v, comecar.token_config())  # estável entre subidas
        self.assertIn(v, buf.getvalue())
        self.assertEqual(oct((self.d / "seg" / comecar.ARQ_TOKEN).stat().st_mode & 0o777), "0o600")
        usuarios.criar("ana@exemplo.test", "senha-forte-123", "dono")
        self.assertIsNone(comecar.anunciar(io.StringIO()))  # com dono não anuncia nada

    def test_escape_de_html(self):
        self.tomlvazio()
        st, _, h = self.passo("dono", {"nome": "<script>x</script>", "email": "ruim", "senha": "senha-forte-123", "senha2": "senha-forte-123"})
        loc = h["Location"]
        self.assertIn("erro=", loc)
        st, corpo, _ = self.req(loc)
        self.assertEqual(st, 200)
        self.assertNotIn("<script>x", corpo)
        self.tomlvazio()
        st, _, h = self.passo("dono", {"nome": '"><img src=x onerror=alert(1)>', "email": "ana@exemplo.test", "senha": "senha-forte-123", "senha2": "senha-forte-123"})
        a = self.sessao_do(h)
        st, corpo, _ = self.req("/comecar?passo=projeto&erro=" + "%3Cb%3Eoi%3C%2Fb%3E", auth=a)
        self.assertNotIn("<b>oi", corpo)
        self.assertIn("&lt;b&gt;oi", corpo)

    def test_senha_ja_existente_nunca_deixa_aberto(self):
        self.tomlvazio()
        (self.d / "seg" / "painel.env").write_text("PAINEL_SENHA=ja-tem-senha\n")  # ainda não migrada
        self.assertEqual(self.req("/pendencias")[0], 303)  # era 401
        self.assertEqual(self.req("/pendencias")[2]["Location"], "/entrar?volta=%2Fpendencias")  # login, não o assistente
        self.assertEqual(self.req("/comecar")[2]["Location"], "/entrar?volta=%2Fcomecar")  # era 401
        (self.d / "seg" / "painel.env").unlink()
        sessao_teste.cookie()  # um usuário já basta
        self.assertEqual(self.req("/comecar")[0], 303)
        self.assertEqual(self.req("/comecar")[2]["Location"], "/entrar?volta=%2Fcomecar")

    def test_post_sem_sessao_com_dono_vai_ao_entrar(self):  # era test_post_sem_basic_com_dono_e_401
        self.tomlvazio('[dono]\nemail = "ana@exemplo.test"\n')
        st, _, h = self.req("/comecar", {"csrf": "abc", "passo": "github", "org": "x"}, cookie="krohnos_comecar=abc")
        self.assertEqual((st, h["Location"]), (303, "/entrar?volta=%2Fcomecar"))
        self.assertEqual(self.req("/comecar", {"csrf": "abc", "passo": "github", "acao": "pular"},
                                  auth=sessao_teste.cookie(papel="leitura"), cookie="krohnos_comecar=abc")[0], 403)
        self.assertEqual(self.req("/comecar", {"csrf": "abc", "passo": "github", "acao": "pular"},
                                  auth=sessao_teste.cookie(), cookie="krohnos_comecar=abc")[0], 303)

    def test_origin_malformado_e_403(self):
        from hub import comecar
        self.assertFalse(comecar.origem_ok("http://[abc", "x"))
        self.tomlvazio()
        import urllib.error
        import urllib.request
        r = urllib.request.Request(self.base + "/comecar", data=b"csrf=abc", headers={"Origin": "http://[abc", "Cookie": "krohnos_comecar=abc"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r)
        self.assertEqual(cm.exception.code, 403)

    def test_senha_forjada_com_quebra_de_linha(self):
        self.tomlvazio()
        st, _, h = self.passo("dono", {"email": "ana@exemplo.test", "senha": "senha-forte\nOUTRA=1", "senha2": "senha-forte\nOUTRA=1"})
        self.assertIn("erro=", h["Location"])
        self.assertFalse((self.d / "seg" / "painel.env").exists())
        self.assertEqual(usuarios.contar(), 0)
        self.assertNotIn("ana@exemplo.test", self.toml.read_text())

    def test_pasta_de_segredos_0700_e_cookie_secure(self):
        self.tomlvazio()
        shutil_dir = self.d / "seg"
        shutil_dir.rmdir()
        a = self.sessao_do(self.passo("dono", {"email": "ana@exemplo.test", "senha": "senha-forte-123", "senha2": "senha-forte-123"})[2])
        # o dono não grava mais nada na pasta de segredos; o token do GitHub, sim (e cria a pasta 0700)
        self.passo("github", {"org": "minha-org", "token": "ghp_" + "a" * 30}, auth=a)
        self.assertEqual(oct(shutil_dir.stat().st_mode & 0o777), "0o700")
        import urllib.request
        r = urllib.request.Request(self.base + "/comecar", headers={"Cookie": a, "X-Forwarded-Proto": "https"})
        self.assertIn("Secure", urllib.request.urlopen(r).headers["Set-Cookie"])

    def test_corrida_no_passo_dono(self):
        import threading
        from hub import comecar
        self.tomlvazio()
        tok = comecar.token_config()
        res = []
        ini = threading.Barrier(6)

        def vai(i):
            ini.wait()
            res.append(comecar.executar("dono", {"email": f"u{i}@exemplo.test", "senha": f"senha-numero-{i}", "senha2": f"senha-numero-{i}",
                                                 "token_config": tok}))
        ts = [threading.Thread(target=vai, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(1 for r in res if r[0] == "ok"), 1)
        self.assertFalse((self.d / "seg" / "painel.env").exists())
        i = re.search(r"u(\d)@", self.toml.read_text()).group(1)
        self.assertEqual([u["email"] for u in usuarios.listar()], [f"u{i}@exemplo.test"])  # usuário e e-mail do mesmo vencedor
        self.assertTrue(usuarios.entrar(f"u{i}@exemplo.test", f"senha-numero-{i}"))
