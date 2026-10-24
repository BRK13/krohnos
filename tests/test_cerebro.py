import datetime as dt
import json
import os
import re
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from unittest.mock import patch

from hub import cerebro, config

AGORA = dt.datetime(2026, 10, 15, 17, 0, tzinfo=dt.timezone.utc)
DESDE = AGORA - dt.timedelta(hours=24)
# O desenho de uma instalação completa (4 contas, Codex, CI, Debugger, servidor, PC e os 6 sites do cadastro de teste),
# dito na config como o dono diz no krohnos.toml dele: nada disso é constante do código.
SITES_DONO = {"loja-a": "Loja A", "loja-b": "Loja B", "loja-c": "Loja C", "loja-d": "Loja D", "loja-e": "Loja E",
              "loja-f": "Loja F"}  # a Loja F sobe direto no servidor (servidor_deploys), como um app fora do Dokploy
CADASTRO_DONO = [{"id": i, "nome": f"{rot} Ltda.", "dominio": f"{i}.exemplo.com", "hosts_alternativos": [f"www.{i}.exemplo.com"],
                  "repo": {"github": f"acme/{i}-site"}, "marca": {"nome": rot, "aliases": []}} for i, rot in SITES_DONO.items()]
DONO = {"KROHNOS_AGENTES_CONTAS": '["1", "2", "3", "4"]', "KROHNOS_CEREBRO_CODEX": "true", "KROHNOS_CEREBRO_CI": "true",
        "KROHNOS_CEREBRO_DEBUGGER": "true", "KROHNOS_CEREBRO_SERVIDOR": "dedicado", "KROHNOS_CEREBRO_SERVIDOR_DEPLOYS": "loja-f",
        "KROHNOS_CEREBRO_GPU_ESTACAO": "/nao/existe/estacao.json", "KROHNOS_CEREBRO_SITES": json.dumps(SITES_DONO),
        "KROHNOS_CEREBRO_REPOS": '{"meu-hub": "dedicado"}', "KROHNOS_CEREBRO_PROJETOS": '{"infra": "dedicado"}',
        "KROHNOS_LINEAR_WORKSPACE": "acme"}


def _site(i, pasta=None):
    for s in CADASTRO_DONO:
        if s["id"] == i:
            return s
    raise cerebro.cadastro.SiteInvalido(i)


def dono(caso, **extra):
    """Liga a topologia do dono (DONO, com o cadastro CADASTRO_DONO) durante o teste `caso`. Código sem o desenho por
    config (antes da) não tem como ler esta topologia: o teste pula, e quem prova a mudança é TopologiaTest."""
    if not hasattr(cerebro, "topologia"):
        raise unittest.SkipTest("Cérebro sem topologia por config")
    for p in (patch.dict(os.environ, {**DONO, **extra}), patch.object(cerebro.cadastro, "ativos", return_value=CADASTRO_DONO),
              patch.object(cerebro.cadastro, "site", _site)):
        p.start()
        caso.addCleanup(p.stop)


def _jsonl(p, linhas):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(x) + "\n" for x in linhas) + "linha quebrada\n", encoding="utf-8")


class EventosDiscoTest(unittest.TestCase):
    def setUp(self):
        dono(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_uso_vira_pegou_e_fim_e_liga_issue_a_conta(self):
        _jsonl(self.base / "claude" / "uso.jsonl", [
            {"inicio": "2026-10-15T16:00:00+00:00", "fim": "2026-10-15T16:10:00+00:00", "conta": "3", "nome": "linear-krn-868",
             "modelo": "opus", "ok": True, "duracao_s": 600},
            {"inicio": "2026-10-15T16:20:00+00:00", "fim": "2026-10-15T16:21:00+00:00", "conta": "2", "nome": "linear-krn-1",
             "ok": False, "erro": "limite"},
            {"inicio": "2026-10-13T10:00:00+00:00", "fim": "2026-10-13T10:05:00+00:00", "conta": "1", "nome": "velha", "ok": True},
            {"inicio": "2026-10-15T16:30:00+00:00", "conta": None, "nome": "agente", "ok": True},  # sem conta: fora do grafo
        ])
        ev, quem = cerebro.eventos_disco(self.base, DESDE, codex=self.base / "nada.jsonl")
        self.assertEqual([(e["tipo"], e["de"], e["para"]) for e in ev],
                         [("pegou", "fila", "c3"), ("fim", "c3", "c3"), ("pegou", "fila", "c2"), ("erro", "c2", "c2")])
        self.assertEqual(quem["KRN-868"], "c3")
        self.assertIn("KRN-868", ev[0]["url"])

    def test_aviso_resposta_codex_e_root(self):
        _jsonl(self.base / "claude" / "avisos-enviados.jsonl", [{"em": "2026-10-15T13:00:00+00:00", "texto": "linha 1\nlinha 2"}])
        _jsonl(self.base / "claude" / "respostas.jsonl", [{"em": "2026-10-15T14:00:40Z", "fase": "orch", "segundos": 40}])
        _jsonl(self.base / "codex.jsonl", [{"em": "2026-10-15T15:00:00+00:00", "repo": "loja-f-site", "pr": 1, "achados": True,
                                            "url": "https://github.com/acme/loja-f-site/pull/1"}])
        r = self.base / "vps" / "resultados" / "x.json"
        r.parent.mkdir(parents=True)
        r.write_text(json.dumps({"em": "2026-10-15T15:30:00+00:00", "acao": "deploy-app", "alvo": "loja-a", "ok": True}))
        ev, _ = cerebro.eventos_disco(self.base, DESDE, codex=self.base / "codex.jsonl")
        tipos = {e["tipo"]: e for e in ev}
        self.assertEqual(tipos["aviso"]["rot"], "Aviso no WhatsApp: linha 1")
        self.assertEqual(tipos["msg"]["t"], "2026-10-15T14:00:00+00:00")  # chegada = resposta - segundos
        self.assertEqual((tipos["codex_achado"]["de"], tipos["codex_achado"]["para"]), ("codex", "ci"))
        self.assertEqual((tipos["pedido"]["de"], tipos["pedido"]["para"]), ("orch", "dedicado"))


class ConsultasGithubTest(unittest.TestCase):
    def test_consulta_org_configurada_e_repos_externos_do_cadastro(self):
        consultas = []
        def gql(query, **variaveis):
            consultas.append(variaveis["q"].split(" is:pr")[0])
            return {"search": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
        with mock.patch.object(cerebro.config, "get", return_value="exemplo"), mock.patch.object(
                cerebro.cadastro, "ativos", return_value=[
                    {"repo": {"github": "exemplo/loja"}},
                    {"repo": {"github": "parceiro/clinica"}}, {"repo": None}]):
            self.assertEqual(cerebro.prs(DESDE, gql), [])
        self.assertEqual(consultas, ["org:exemplo", "repo:parceiro/clinica"])


class EventosGithubTest(unittest.TestCase):
    def test_pr_commit_ci_e_merge_com_a_conta_certa(self):
        pr = {"number": 7, "title": "Cérebro (KRN-868)", "url": "u", "headRefName": "x", "repository": {"name": "nimbus-hub"},
              "createdAt": "2026-10-15T16:00:00Z", "mergedAt": "2026-10-15T16:40:00Z",
              "commits": {"nodes": [{"commit": {"oid": "abcdef0123", "committedDate": "2026-10-15T15:59:00Z", "status": {"contexts": [
                  {"context": "local-ci", "state": "PENDING", "createdAt": "2026-10-15T16:01:00Z"},
                  {"context": "local-ci", "state": "FAILURE", "createdAt": "2026-10-15T16:20:00Z"},
                  {"context": "outro", "state": "SUCCESS", "createdAt": "2026-10-15T16:21:00Z"}]}}}]}}
        ev = cerebro.eventos_github([pr], DESDE, {"KRN-868": "c4"})
        self.assertEqual([(e["tipo"], e["de"], e["para"]) for e in ev],
                         [("pr", "c4", "codex"), ("commit", "c4", "ci"), ("ci_falha", "ci", "c4"), ("merge", "ci", "deploy")])


class GrafoTest(unittest.TestCase):
    def setUp(self):
        dono(self)
        leitura_sessoes = mock.patch.object(cerebro, "CODEX_SESSOES", Path("/inexistente"))
        leitura_sessoes.start()
        self.addCleanup(leitura_sessoes.stop)
        leitura = patch("hub.orq_estado.limite_codex", return_value=None)
        leitura.start()
        self.addCleanup(leitura.stop)

    def test_arestas_ligam_nos_que_existem(self):
        t = cerebro.topologia()
        ids = {n[0] for n in cerebro.layout(t)}
        self.assertTrue(all(a in ids and b in ids for a, b in cerebro.arestas_fixas(t)))
        self.assertTrue(set(t["repo_no"].values()) <= ids and set(t["projeto_no"].values()) <= ids)
        # repo → nó e projeto → nó saem do cadastro (repo.github, nome do site), não de tabela no código
        self.assertEqual((t["repo_no"]["loja-f-site"], t["repo_no"]["loja-a-site"], t["repo_no"]["meu-hub"]),
                         ("loja-f", "loja-a", "dedicado"))
        self.assertEqual((t["projeto_no"]["lojaaltda"], t["projeto_no"]["lojad"], t["projeto_no"]["infra"]),
                         ("loja-a", "loja-d", "dedicado"))

    def test_nos_sem_dados_nao_inventam_alerta(self):
        with tempfile.TemporaryDirectory() as d:
            nos = cerebro.nos(d, AGORA)
        self.assertEqual(len(nos), len(cerebro.layout(cerebro.topologia())))
        self.assertFalse(any(n["alerta"] for n in nos))
        self.assertIn("sem leitura", " ".join(next(n for n in nos if n["id"] == "loja-a")["linhas"]))


    def test_conta_estourada_mostra_a_volta(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "claude").mkdir()
            reset = (AGORA + dt.timedelta(hours=2)).isoformat()
            (Path(d) / "claude" / "uso-contas.json").write_text(json.dumps({"contas": {
                "1": {"cinco_horas": {"pct": 100, "reset": reset}}, "2": {"cinco_horas": {"pct": 40, "reset": reset}}}}))
            (Path(d) / "claude" / "pausa-3.json").write_text(json.dumps({"ate": reset}))
            por = {n["id"]: n for n in cerebro.nos(d, AGORA)}
        volta = (AGORA + dt.timedelta(hours=-1)).strftime("%H:%M")
        self.assertEqual(por["c1"]["cota"], 100)
        self.assertNotIn("semana", por["c1"])
        self.assertNotIn("semana_pct", por["c1"])
        self.assertIsNone(por["c3"]["cota"])
        self.assertEqual(por["c1"]["estourou"], volta)
        self.assertEqual(por["c3"]["estourou"], volta)
        self.assertEqual(por["c2"]["estourou"], "")
        self.assertIn(f"Limite estourado · volta {volta}", por["c1"]["linhas"])

    def test_codex_cota_semanal_independente_das_tarefas(self):
        with tempfile.TemporaryDirectory() as d:
            estado = Path(d) / "orch" / "estado.json"
            estado.parent.mkdir()
            sessoes = Path(d) / "claude"
            sessoes.mkdir()
            reset = AGORA + dt.timedelta(days=3, hours=2)
            for ativo in (0, 1):
                if ativo:
                    (sessoes / "rodando-codex.json").write_text(json.dumps({
                        "nome": "linear-krn-911", "conta": "codex", "inicio": AGORA.isoformat(), "pid": 1}))
                for pct in (0, 70, 90, 100):
                    with self.subTest(ativo=ativo, pct=pct):
                        estado.write_text(json.dumps({"codex": {"semana": {
                            "pct": pct, "reset": reset.isoformat()}}}))
                        cx = next(n for n in cerebro.nos(d, AGORA) if n["id"] == "codex")
                        self.assertEqual((cx["cota"], cx["semana"], cx["ativo"]), (pct, pct, ativo))
                        self.assertEqual(cx["carga"], pct / 100)
                        self.assertEqual(cx["estourou"], "18/10 16:00" if pct == 100 else "")

    def test_codex_mostra_o_que_roda(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "claude").mkdir()
            for i, nome in enumerate(("linear-krn-864", "codex-revisor-merge")):
                (Path(d) / "claude" / f"rodando-codex{'-v1' if i else ''}.json").write_text(json.dumps(
                    {"nome": nome, "conta": "codex", "inicio": AGORA.isoformat(), "pid": 1}))
            cx = next(n for n in cerebro.nos(d, AGORA) if n["id"] == "codex")
        self.assertEqual(cx["ativo"], 2)

class AoVivoTest(unittest.TestCase):
    FORA = {"prs": [], "linear": [], "sites": None}

    def setUp(self):
        dono(self)
        leitura = patch("hub.orq_estado.limite_codex", return_value=None)
        leitura.start()
        self.addCleanup(leitura.stop)

    def test_desde_reenvia_so_a_janela_de_atraso(self):
        with tempfile.TemporaryDirectory() as d:
            _jsonl(Path(d) / "claude" / "avisos-enviados.jsonl", [{"em": "2026-10-15T10:00:00+00:00", "texto": "velho"},
                                                                  {"em": "2026-10-15T16:40:00-03:00", "texto": "fuso"},
                                                                  {"em": "2026-10-15T16:50:00Z", "texto": "novo"}])
            tudo = cerebro.ao_vivo(base=d, agora=AGORA, fora=self.FORA)
            parte = cerebro.ao_vivo("2026-10-15T17:10:00+00:00", base=d, agora=AGORA, fora=self.FORA)
        self.assertEqual([e["rot"][-5:] for e in tudo["eventos"]], ["velho", " novo"])  # 16:40-03 = 19:40 UTC: futuro, fora
        self.assertEqual([e["rot"][-4:] for e in parte["eventos"]], ["novo"])
        self.assertTrue(all(e["t"].endswith("+00:00") for e in tudo["eventos"]))

    def test_fora_le_uma_vez_por_minuto_e_guarda_a_ultima_boa(self):
        chamadas = []

        def falha(*_):
            chamadas.append(1)
            raise OSError("gh fora")
        orig = (cerebro.prs, cerebro.eventos_linear, cerebro.sites_agora)
        cerebro._FORA.update(em=None, prs=["antigo"])
        cerebro.prs = cerebro.eventos_linear = cerebro.sites_agora = falha
        try:
            a = cerebro._fora(AGORA)
            cerebro._fora(AGORA)
        finally:
            cerebro.prs, cerebro.eventos_linear, cerebro.sites_agora = orig
            cerebro._FORA.update(em=None, prs=[])
        self.assertEqual(len(chamadas), 3)  # 2ª chamada no mesmo minuto: cache
        self.assertEqual(a["prs"], ["antigo"])

    def test_html_embute_dados_e_modo(self):
        h = cerebro.html({"x": "</script>"}, "/cerebro.json", True)
        self.assertIn('const URL_AO_VIVO = "/cerebro.json"', h)
        self.assertIn("const COMPACTO = true", h)
        self.assertNotIn("</script>\"}", h)
        self.assertIn("const URL_AO_VIVO = null", cerebro.html({}))

    def test_espaco_three_fixo_teto_de_fps_e_fallback(self):
        """Three.js em versão fixa pelo CDN, sem @latest; teto de 30 fps; sem WebGL marca o desenho 2D."""
        h = cerebro.html({})
        self.assertIn('const THREE_URL = "https://cdn.jsdelivr.net/npm/three@0.186.0/build/three.module.js"', h)
        self.assertIn("import(THREE_URL)", h)
        self.assertNotIn("three@latest", h)
        self.assertEqual(re.findall(r"three@([^/\"']*)", h), ["0.186.0"])   # uma só versão, exata
        self.assertNotIn('type="importmap"', h)
        self.assertRegex(h, r"\bFPS_MAX = 30\b")
        self.assertIn("1/FPS_MAX", h)
        self.assertIn("Math.min(devicePixelRatio || 1, 1.5)", h)
        # começa estático e só vira "webgl" depois que o import e o renderer dão certo; perda de contexto volta ao 2D
        self.assertIn('marcarVisual("estatico");', h)
        self.assertIn('cv.dataset.visual = v', h)
        self.assertIn("webglcontextlost", h)
        self.assertNotIn("const energia = ", h)   # a fita de energia antiga saiu

    def test_sem_lag_polling_15s_aba_oculta_movimento_reduzido_um_webgl(self):
        """polling de 15 s, laço parado com a aba oculta, movimento reduzido estático, um só contexto WebGL."""
        h = cerebro.html({}, "/cerebro.json")
        self.assertIn("const POLL_MS = 15000", h)
        self.assertIn("setInterval(buscarVivo, POLL_MS)", h)
        self.assertNotIn("setInterval(buscarVivo, 5000)", h)
        self.assertIn("visibilitychange", h)
        self.assertIn("prefers-reduced-motion", h)
        self.assertIn("ultimaAssinatura", h)   # só redesenha se o JSON mudou
        self.assertEqual(len(re.findall(r"new T\.WebGLRenderer\(", h)), 1)   # um contexto, não um por planeta
        self.assertEqual(len(re.findall(r"getContext\(\s*[\"']webgl2?[\"']", h)), 0)
        self.assertIn("reduzir.matches || COMPACTO", h)   # quadro compacto sem animação nem WebGL
        for velho in ("vps8238", "gemea", "82.38", "Gêmea", "VPS"):
            self.assertNotIn(velho, h)
        self.assertIsNone(re.search(r"\bPrec\d\b", h))   # os dois servidores antigos do coletor


class PainelTest(unittest.TestCase):
    def test_rotas_do_cerebro(self):
        from hub import painel
        d = {"agora": "a", "desde": "b", "nos": [], "arestas": [], "eventos": []}
        orig = cerebro.ao_vivo
        cerebro.ao_vivo = lambda desde=None, **_: dict(d, pedido=desde)
        try:
            st, corpo, cab = painel.rota("/cerebro?compacto=1")
            st_j, j, cab_j = painel.rota("/cerebro.json?desde=2026-10-15T17:00:00%2B00:00")
        finally:
            cerebro.ao_vivo = orig
        self.assertEqual((st, cab["X-Frame-Options"]), (200, "SAMEORIGIN"))  # só leitura: a tela inicial embute
        self.assertIn("const COMPACTO = true", corpo)
        self.assertEqual((st_j, json.loads(j)["pedido"]), (200, "2026-10-15T17:00:00+00:00"))
        self.assertIn("application/json", cab_j["Content-Type"])

class CotasTest(unittest.TestCase):
    def setUp(self):
        dono(self)

    def test_janelas_independentes_pausa_e_codex(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / 'claude').mkdir()
            reset = (AGORA + dt.timedelta(hours=2)).isoformat()
            (base / 'claude' / 'uso-contas.json').write_text(json.dumps({'contas': {
                '1': {'cinco_horas': {'pct': 0}, 'semana': {'pct': 99}},
                '2': {'cinco_horas': {'pct': 75}},
                '3': {'cinco_horas': {'pct': 100, 'reset': reset}}}}))
            with patch('hub.orq_estado.limite_codex', return_value={'pct': 100, 'reset': reset}):
                por = {n['id']: n for n in cerebro.nos(base, AGORA)}
            self.assertEqual((por['c1']['cota_pct'], por['c2']['cota_pct']), (0, 75))
            self.assertNotIn('semana_pct', por['c1'])  # Claude sem limite semanal, mesmo com 99 no JSON
            self.assertNotIn('semana', por['c1'])
            self.assertIsNone(por['c4']['cota_pct'])
            self.assertEqual(por['codex']['cota_pct'], 100)
            self.assertEqual(por['codex']['semana_pct'], 100)
            self.assertEqual(por['codex']['estourou'], por['c3']['estourou'])
            self.assertTrue(por['codex']['estourou'])


    def test_falha_de_leitura_codex_preserva_grafo_e_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "orch").mkdir()
            estado = base / "orch" / "estado.json"
            for erro in (OSError("sem acesso"), ValueError("JSON quebrado")):
                with patch("hub.orq_estado.limite_codex", side_effect=erro):
                    por = {n["id"]: n for n in cerebro.nos(base, AGORA)}
                    self.assertEqual(len(por), len(cerebro.layout(cerebro.topologia())))
                    self.assertIsNone(por["codex"]["cota_pct"])
                    estado.write_text(json.dumps({"codex": {"semana": {"pct": 82}}}))
                    por = {n["id"]: n for n in cerebro.nos(base, AGORA)}
                    self.assertEqual(por["codex"]["cota_pct"], 82)
                    estado.unlink()

    def test_reset_semanal_mostra_data_e_hora(self):
        reset = AGORA + dt.timedelta(days=3, hours=2)
        with tempfile.TemporaryDirectory() as d:
            with patch("hub.orq_estado.limite_codex", return_value={"pct": 100, "reset": reset.isoformat()}):
                por = {n["id"]: n for n in cerebro.nos(d, AGORA)}
        self.assertEqual(por["codex"]["estourou"], "18/10 16:00")


class FontesTest(unittest.TestCase):
    def setUp(self):
        dono(self)

    def test_nos_correspondem_as_fontes_e_ignoram_pid_morto(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            def gravar(nome, d):
                p = base / nome
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(d))
                return p
            uso = {str(i): {'cinco_horas': {'pct': p, 'reset': (AGORA + dt.timedelta(hours=2)).isoformat()},
                            } for i,p in enumerate([27,0,8,100],1)}
            gravar('claude/uso-contas.json', {'contas': uso})
            tarefas = [('1','krn-828'),('2','krn-667'),('2','krn-848'),('3','krn-905'),
                       ('4','consertador'),('codex','krn-893'),('codex','debugger')]
            for i,(c,n) in enumerate(tarefas):
                gravar(f'claude/rodando-{i}.json', {'conta':c,'nome':n,'pid':1})
            gravar('claude/rodando-morto.json', {'conta':'4','nome':'morto','pid':999999999})
            gpu = gravar('estacao.json', {'visto_em': (AGORA-dt.timedelta(minutes=2)).isoformat(),
                                         'gpu':'RTX 4070 Laptop','ocupada':False})
            gravar('monitor/remotos.json', {s:{'cpu':41,'mem_pct':62,'disco_pct':73,
                    't':(AGORA-dt.timedelta(minutes=3)).isoformat()} for s in ['dedicado']})
            sessao = gravar('sessions/a.jsonl', {'payload': {'rate_limits': {
                'primary': {'used_percent':12,'window_minutes':300},
                'secondary': {'used_percent':88,'window_minutes':10080}}}})
            with mock.patch.dict(os.environ, {'KROHNOS_CEREBRO_GPU_ESTACAO': str(gpu)}), mock.patch.object(cerebro,'CODEX_SESSOES',sessao.parent):
                por = {n['id']:n for n in cerebro.nos(base,AGORA,{'loja-b':{'status':503},'loja-c':{'status':200}})}
                self.assertEqual(por['pc']['linhas'], ['ligado · ocioso','RTX 4070 Laptop'])
                self.assertEqual(por['codex']['cota'],88)
                for i,ativo in enumerate([1,2,1,1],1):
                    n=por[f'c{i}']; self.assertEqual(n['ativo'],ativo)
                    self.assertEqual(n['cota'],uso[str(i)]['cinco_horas']['pct'])
                    self.assertIn(f"5 h: {n['cota']:.0f}%",n['linhas'][-1])
                self.assertEqual(por['codex']['ativo'],2)
                self.assertEqual((por['dedicado']['linhas'] or [''])[0],'CPU 41% · RAM 62% · disco 73% · há 3 min')
                self.assertFalse(por['dedicado']['alerta'])
                self.assertNotIn('vps8238', por); self.assertNotIn('gemea', por)   # 82.38 e gêmea saíram do desenho
                self.assertIn('fora do ar',por['loja-b']['linhas'])
                self.assertIn('HTTP 200',por['loja-c']['linhas'])
                self.assertTrue(por['c4']['estourou'])
                for minutos, texto in [(7,'sinal atrasado'),(16,'desligado')]:
                    gpu.write_text(json.dumps({'visto_em':(AGORA-dt.timedelta(minutes=minutos)).isoformat()}))
                    self.assertEqual(next(n for n in cerebro.nos(base,AGORA) if n['id']=='pc')['linhas'],[texto])


class PlacarTest(unittest.TestCase):
    def setUp(self):
        dono(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _uso(self):
        def r(fim, conta, nome, modelo, ok=True, custo=.5, origem="tarefa", tipo=None):
            return {"inicio": fim, "fim": fim, "conta": conta, "nome": nome, "modelo": modelo, "ok": ok, "custo_usd": custo,
                    "turnos": 3, "origem": origem, "tipo": tipo}
        _jsonl(self.base / "claude" / "uso.jsonl", [
            r("2026-10-15T16:00:00+00:00", "1", "linear-krn-1", "claude-sonnet-5-5"),
            r("2026-10-15T16:10:00+00:00", "2", "linear-krn-2", "opus", ok=False, custo=1.5),
            r("2026-10-15T16:20:00+00:00", "2", "linear-krn-2", "opus", tipo="conferencia"),
            r("2026-10-15T16:30:00+00:00", None, "orch", "haiku", origem="agente", custo=None),
            r("2026-10-13T10:00:00+00:00", "1", "linear-krn-9", "opus"),  # fora das 24 h
        ])

    def test_placar_de_dado_real_e_sem_leitura(self):
        self._uso()
        p = cerebro.placar(self.base, AGORA, [], fila_n=4)
        et = {e["no"]: e for e in p["etapas"]}
        self.assertEqual({m["id"]: m["n"] for m in p["modelos"]}, {"opus": 2, "sonnet": 1, "haiku": 1, "codex": 0})
        self.assertEqual((et["c1"]["ok"], et["c1"]["falha"], et["c1"]["grande"]), (2, 1, "66%"))  # 66,6% não vira 67
        self.assertIn("US$ 0,83/tarefa", et["c1"]["linhas"][1])
        self.assertIn("retrabalho 50% (1 de 2)", et["c1"]["linhas"][2])
        self.assertEqual(et["fila"]["grande"], "4")
        self.assertEqual(et["orch"]["linhas"][1], "custo/execução: sem leitura")  # custo None não vira zero
        self.assertEqual(et["ci"]["grande"], "sem leitura")
        self.assertIn("nenhum local-ci", et["ci"]["linhas"][0])

    def test_log_e_rodape(self):
        self._uso()
        p = cerebro.placar(self.base, AGORA, [])
        self.assertEqual(len(p["log"]), 4)
        self.assertEqual((p["log"][0]["conta"], p["log"][0]["hora"]), ("hub", "13:30:00"))  # BRT
        self.assertEqual(p["log"][1]["modelo"], "opus")
        self.assertEqual((p["rodape"]["ok"], p["rodape"]["falha"]), (3, 1))
        self.assertEqual(p["rodape"]["custo_txt"], "US$ 2,50")
        self.assertEqual(cerebro.placar(self.base / "vazio", AGORA, [])["log"], [])


class ServidoresTest(unittest.TestCase):
    """o desenho é a infra da virada de 22/10 e cada site liga ao servidor que o atende de verdade."""

    def setUp(self):
        dono(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _remotos(self, d):
        arq = self.base / "monitor" / "remotos.json"
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text(json.dumps(d))

    def test_um_no_so_servidor_sem_82_38_nem_gemea(self):
        """Decisão do Fulano (22/10): "mata os 3, deixa só o servidor" — um nó de servidor (o dedicado), PC fica."""
        t = cerebro.topologia()
        infra = {n[0]: n[1] for n in cerebro.layout(t) if n[2] == "infra"}
        self.assertEqual(infra, {"pc": "PC · GPU", "dedicado": "Servidor"})
        fixas = cerebro.arestas_fixas(t)
        self.assertFalse(any(a in ("vps8238", "gemea") or b in ("vps8238", "gemea") for a, b in fixas))
        for par in [("orch", "dedicado"), ("c4", "dedicado"), ("codex", "dedicado"), ("loja-f", "dedicado")]:
            self.assertIn(par, fixas)

    def test_site_liga_ao_servidor_que_atende(self):
        self._remotos({"dedicado": {"sites": ["www.loja-a.exemplo.com", "loja-d.exemplo.com"]}})
        d = cerebro.montar(self.base, agora=AGORA, fora={"prs": [], "linear": [], "sites": None})
        ar = {tuple(a) for a in d["arestas"]}
        self.assertIn(("loja-a", "dedicado"), ar)    # pelo host alternativo (www.) do cadastro
        self.assertIn(("loja-d", "dedicado"), ar)
        self.assertIn(("loja-f", "dedicado"), ar)    # sobe direto no servidor (servidor_deploys)
        self.assertNotIn(("loja-b", "dedicado"), ar)   # a Loja B não está no servidor lido: sem linha para ela
        por = {n["id"]: n for n in d["nos"]}
        self.assertNotIn("vps8238", por)
        self.assertIn("Sites: Loja A, Loja D", por["dedicado"]["linhas"])

    def test_servidor_sem_resposta_mostra_a_idade_e_alerta(self):
        self._remotos({"dedicado": {"cpu": 1.3, "mem_pct": 6.7, "disco_pct": 69, "t": (AGORA - dt.timedelta(minutes=252)).isoformat(),
                                    "erro": "sem resposta"}})
        g = {n["id"]: n for n in cerebro.nos(self.base, AGORA)}["dedicado"]
        self.assertTrue(g["alerta"])
        self.assertIn("há 252 min", g["linhas"][0])
        self.assertIn("Sem resposta: sem resposta", g["linhas"])


class SitesSemLeituraTest(unittest.TestCase):
    """o painel ficou horas com os 6 sites "fora do ar" porque a leitura dele falhava inteira."""

    def setUp(self):
        dono(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cerebro._FORA.update(sites=None)
        self.addCleanup(cerebro._FORA.update, sites=None)

    def _linhas(self, sites):
        return {n["id"]: n for n in cerebro.nos(self.tmp.name, AGORA, sites) if n["grupo"] == "site"}

    def test_todos_none_nao_vira_fora_do_ar(self):
        por = self._linhas({s: {"status": None, "versao": ""} for s in SITES_DONO})
        self.assertFalse(any(n["alerta"] for n in por.values()))
        self.assertTrue(all("sem leitura (painel)" in n["linhas"] for n in por.values()))

    def test_um_none_vira_fora_do_ar(self):
        sites = {s: {"status": 200, "versao": ""} for s in SITES_DONO}
        sites["loja-b"] = {"status": None, "versao": ""}
        por = self._linhas(sites)
        self.assertTrue(por["loja-b"]["alerta"])
        self.assertIn("fora do ar", por["loja-b"]["linhas"])
        self.assertFalse(por["loja-a"]["alerta"])

    def test_rodada_toda_none_guarda_a_leitura_boa_e_tenta_processo_novo(self):
        boa = {s: {"status": 200, "versao": "", "em": "2026-10-15T16:50:00+00:00"} for s in SITES_DONO}
        cerebro._FORA["sites"] = boa
        ruim = lambda: {s: {"status": None, "versao": ""} for s in SITES_DONO}  # noqa: E731
        tentou = []
        self.assertTrue(hasattr(cerebro, "_ler_sites"), "sem guarda para a rodada toda None")
        s = cerebro._ler_sites(AGORA, ler=ruim, novo=lambda: tentou.append(1))
        self.assertEqual(tentou, [1])
        self.assertIs(s, boa)
        n = self._linhas(s)["loja-a"]
        self.assertIn("HTTP 200 · leitura de 10 min atrás", n["linhas"])
        # o processo novo leu: vale a leitura dele
        s = cerebro._ler_sites(AGORA, ler=ruim, novo=lambda: {x: {"status": 200, "versao": ""} for x in SITES_DONO})
        self.assertEqual(s["loja-a"], {"status": 200, "versao": "", "em": AGORA.isoformat(timespec="seconds")})



class SitesLeituraVelhaTest(unittest.TestCase):
    def setUp(self):
        dono(self)
        self.addCleanup(cerebro._FORA.update, sites=None)

    def _boa(self, min_atras):
        em = (AGORA - dt.timedelta(minutes=min_atras)).isoformat()
        return {s: {"status": 200, "versao": "", "em": em} for s in SITES_DONO}

    def test_leitura_boa_com_mais_de_30_min_nao_vale(self):
        ruim = lambda: {s: {"status": None, "versao": ""} for s in SITES_DONO}  # noqa: E731
        cerebro._FORA["sites"] = self._boa(31)
        s = cerebro._ler_sites(AGORA, ler=ruim, novo=lambda: None)
        self.assertTrue(all(v["status"] is None for v in s.values()))
        cerebro._FORA["sites"] = boa = self._boa(29)
        self.assertIs(cerebro._ler_sites(AGORA, ler=ruim, novo=lambda: None), boa)


class TopologiaTest(unittest.TestCase):
    """o grafo nasce da config e do cadastro de quem instalou; o código não tem a topologia de ninguém.
    Só pela API pública de sempre (montar, eventos_disco): o mesmo teste vale contra o código antigo e falha nele."""
    SEM_SITES = "_sites"   # o id do lugar dos sites numa instalação sem cadastro ("configure seus sites")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        for p in (patch("hub.orq_estado.limite_codex", return_value=None), patch.object(cerebro, "CODEX_SESSOES", Path("/inexistente"))):
            p.start()
            self.addCleanup(p.stop)

    def _cadastro(self, n):
        """n sites ativos (fictícios) no cadastro."""
        sites = [{"id": f"loja-{i}", "nome": f"Loja {i}", "dominio": f"loja-{i}.exemplo.com", "hosts_alternativos": [],
                  "repo": {"github": f"acme/loja-{i}"}, "marca": {"nome": f"Loja {i}", "aliases": []}} for i in range(n)]

        def site(i, pasta=None):
            for s in sites:
                if s["id"] == i:
                    return s
            raise cerebro.cadastro.SiteInvalido(i)
        p = [patch.object(cerebro.cadastro, "ativos", return_value=sites), patch.object(cerebro.cadastro, "site", site)]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def _grafo(self):
        d = cerebro.montar(self.base, agora=AGORA, fora={"prs": [], "linear": [], "sites": None})
        return d, {n["id"]: n for n in d["nos"]}

    def test_sem_config_nem_cadastro_uma_conta_e_nenhum_site(self):
        with patch.object(cerebro.cadastro, "ativos", side_effect=cerebro.cadastro.SiteInvalido("nenhum site")):
            d, por = self._grafo()
        self.assertEqual([n["id"] for n in d["nos"] if n["grupo"] == "conta"], ["c1"])
        self.assertEqual([n for n in d["nos"] if n["grupo"] == "site"], [])
        self.assertEqual(list(por), ["voce", "orch", "fila", "c1", self.SEM_SITES])   # Você → Orquestrador → Fila → Conta 1
        self.assertEqual(por[self.SEM_SITES]["rot"], "Configure seus sites")
        self.assertEqual(d["placar"]["rodape"]["contas_total"], 1)
        self.assertEqual({tuple(a) for a in d["arestas"]}, {("voce", "orch"), ("voce", "fila"), ("orch", "fila"), ("fila", "c1"), ("c1", "orch")})

    def test_exemplos_so_com_demo(self):
        """Sem cadastro o hub cai em exemplos/projetos (padrão neutro): só vira site no mapa com o `krohnos demo` ligado."""
        from hub import demo, sites
        exemplos = cerebro.RAIZ / "exemplos" / "projetos"
        with patch.object(sites, "PASTA", exemplos):
            with patch.object(demo, "ativa", return_value=False):
                _, por = self._grafo()
                self.assertEqual([n for n in por.values() if n["grupo"] == "site"], [])
            with patch.object(demo, "ativa", return_value=True):
                d, _ = self._grafo()
                self.assertEqual(sorted(n["rot"] for n in d["nos"] if n["grupo"] == "site"), ["Clínica Exemplo", "Loja Exemplo"])

    def test_duas_contas_e_tres_sites(self):
        self._cadastro(3)
        with patch.dict(os.environ, {"KROHNOS_AGENTES_CONTAS": '["1", "2"]'}):
            d, por = self._grafo()
        self.assertEqual([n["id"] for n in d["nos"] if n["grupo"] == "conta"], ["c1", "c2"])
        self.assertEqual([(n["id"], n["rot"]) for n in d["nos"] if n["grupo"] == "site"],
                         [("loja-0", "Loja 0"), ("loja-1", "Loja 1"), ("loja-2", "Loja 2")])   # rótulo = nome do cadastro
        self.assertNotIn(self.SEM_SITES, por)
        self.assertEqual([round(n["y"], 2) for n in d["nos"] if n["grupo"] == "site"], [.07, .42, .77])   # y pelo número real
        self.assertEqual(d["placar"]["rodape"]["contas_total"], 2)
        self.assertTrue(all(a in por and b in por for a, b in d["arestas"]))
        self.assertIn(("deploy", "loja-2"), {tuple(a) for a in d["arestas"]})

    def test_lista_de_sites_da_config_e_fechada(self):
        """[cerebro.sites] presente: só os ids dela, na ordem dela (o 4º site ativo do cadastro fica fora do desenho)."""
        self._cadastro(4)
        with patch.dict(os.environ, {"KROHNOS_CEREBRO_SITES": '{"loja-2": "Dois", "loja-0": "Zero", "loja-1": "Um"}'}):
            d, por = self._grafo()
        self.assertEqual([(n["id"], n["rot"]) for n in d["nos"] if n["grupo"] == "site"],
                         [("loja-2", "Dois"), ("loja-0", "Zero"), ("loja-1", "Um")])
        self.assertNotIn("loja-3", por)
        self.assertFalse(any("loja-3" in a for a in d["arestas"]))

    def test_evento_de_no_fora_do_desenho_nao_vai_a_tela(self):
        """Sem Codex nem CI no desenho: revisão do Codex, PR → codex, commit → ci e merge ci → deploy saem dos eventos."""
        self._cadastro(1)
        rev = self.base / "revisoes.jsonl"
        _jsonl(rev, [{"em": "2026-10-15T15:00:00+00:00", "repo": "loja-0", "pr": 1, "achados": False}])
        pr = {"number": 7, "title": "Algo (KRN-868)", "url": "u", "headRefName": "x", "repository": {"name": "loja-0"},
              "createdAt": "2026-10-15T16:00:00Z", "mergedAt": "2026-10-15T16:40:00Z",
              "commits": {"nodes": [{"commit": {"oid": "abcdef0123", "committedDate": "2026-10-15T15:59:00Z", "status": {"contexts": [
                  {"context": "local-ci", "state": "SUCCESS", "createdAt": "2026-10-15T16:20:00Z"}]}}}]}}
        _jsonl(self.base / "claude" / "uso.jsonl", [{"inicio": "2026-10-15T15:00:00+00:00", "conta": "1", "nome": "linear-krn-868"}])
        with patch.dict(os.environ, {"KROHNOS_CEREBRO_CODEX_REVISOES": str(rev)}):
            d = cerebro.montar(self.base, agora=AGORA, fora={"prs": [pr], "linear": [], "sites": None})
        ids = {n["id"] for n in d["nos"]}
        self.assertTrue(d["eventos"])   # o "pegou" da conta 1 fica
        self.assertEqual([(e["tipo"], e["de"], e["para"]) for e in d["eventos"] if e["de"] not in ids or e["para"] not in ids], [])
        self.assertEqual({e["tipo"] for e in d["eventos"]}, {"pegou"})

    def test_sem_config_nenhum_no_fixo_de_infra(self):
        fixos = {"codex", "ci", "debugger", "pc", "dedicado"}
        self._cadastro(2)
        d, por = self._grafo()
        self.assertEqual(sorted(fixos & set(por)), [])
        self.assertEqual([n for n in d["nos"] if n["grupo"] == "infra"], [])
        self.assertFalse(any(a in fixos or b in fixos for a, b in d["arestas"]))
        # sem caminho configurado não há leitura de revisão do Codex nem alvo de servidor nos eventos
        r = self.base / "vps" / "resultados" / "x.json"
        r.parent.mkdir(parents=True)
        r.write_text(json.dumps({"em": "2026-10-15T15:30:00+00:00", "acao": "deploy-app", "ok": True}))
        ev, _ = cerebro.eventos_disco(self.base, DESDE)
        self.assertEqual([(e["de"], e["para"]) for e in ev if e["de"] in fixos or e["para"] in fixos], [])
        self.assertEqual([(e["de"], e["para"]) for e in ev], [("orch", "orch")])   # a ação do root fica no orquestrador

    def test_infra_e_esteiras_so_com_a_chave(self):
        self._cadastro(1)
        with patch.dict(os.environ, {"KROHNOS_CEREBRO_SERVIDOR": "srv-1", "KROHNOS_CEREBRO_DEBUGGER": "true",
                                     "KROHNOS_AGENTES_FAIXAS": '{"1": "construtor", "2": "codex"}'}):
            d, por = self._grafo()
        self.assertIn("srv-1", por)
        self.assertEqual((por["srv-1"]["grupo"], por["srv-1"].get("papel")), ("infra", "servidor"))
        self.assertIn("codex", por)            # faixa codex configurada liga o nó
        self.assertIn("debugger", por)
        self.assertNotIn("ci", por)            # sem a esteira de CI, sem o nó
        ar = {tuple(a) for a in d["arestas"]}
        self.assertIn(("debugger", "orch"), ar)
        self.assertIn(("c1", "deploy"), ar)    # sem CI, a conta entrega direto no deploy
        self.assertTrue(all(a in por and b in por for a, b in ar))

    def test_codigo_sem_topologia_nem_caminho_do_dono(self):
        """Nenhuma tabela de topologia no módulo, nenhum caminho absoluto e nenhum id de site do cadastro escrito no código."""
        for nome in ("CONTAS", "SITES", "SERVIDORES", "HUB", "REPO_NO", "PROJETO_NO", "LAYOUT", "ARESTAS", "CODEX_REVISOES", "GPU_ESTACAO"):
            self.assertFalse(hasattr(cerebro, nome), nome)
        fonte = (cerebro.RAIZ / "hub" / "cerebro.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"[\"']/(?:home|srv|opt|var)/[^\"']*", fonte), [])
        ids = [s["id"] for s in cerebro.cadastro.ativos()]
        self.assertTrue(ids)
        self.assertEqual([i for i in ids if f'"{i}"' in fonte or f"'{i}'" in fonte], [])
        self.assertNotIn(f'"{DONO["KROHNOS_CEREBRO_SERVIDOR"]}"', fonte)   # o id do servidor é da config

if __name__ == "__main__":
    unittest.main()
