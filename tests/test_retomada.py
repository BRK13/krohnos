"""retomada sem IA de verdade. Tipo obrigatório, teto de execuções por tarefa, gancho e conferência por código,
causa do despertar da espera do CI, log do job vermelho e a meta medida. Nada fala com GitHub, Linear nem a CLI."""
import datetime as dt
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import agenda, conta, esperas, linear, painel, parada, retomada, tokens as TK
from tests.test_esperas import PR, SHA, T0, GitHubFalso, ci

UTC = dt.timezone.utc
AGORA = dt.datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
ITEM = {"identifier": "KRN-77", "id": "uuid-77", "tipo": "issue"}
NOME = "linear-krn-77"


def uso(nome, minutos_atras, tipo="espera_ci", custo=1.0, dur=120, **extra):
    return {"inicio": conta.iso(AGORA - dt.timedelta(minutes=minutos_atras)), "nome": nome, "tipo": tipo,
            "custo_usd": custo, "duracao_s": dur, "origem": "tarefa", **extra}


class DadosTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dados = tmp.name
        for p in (mock.patch.dict(os.environ, {"HUB_DATA": tmp.name}), mock.patch("hub.conta.agora", lambda: AGORA)):
            p.start()
            self.addCleanup(p.stop)

    def registrar(self, n, **k):
        for i in range(n):
            conta.registrar(uso(NOME, 10 + i, **k), self.dados)


class TipoObrigatorioTest(unittest.TestCase):
    def test_inc_317_in_review_parada_bru_1528(self):
        # uso.jsonl: 2026-10-22T04:19:02+00:00, encerrada às 04:19:34.
        for horas in (2, 12, 48):
            motivo = f"In Review parada há {horas} h com conta ociosa (KRN-1600)"
            with self.subTest(horas=horas):
                registrar = mock.Mock()
                tipo = TK.tipo_execucao(motivo, {"identifier": "KRN-1528"})
                self.assertEqual("parada", tipo)
                self.assertEqual("parada", retomada.exigir_tipo(tipo, "linear-krn-1528", registrar=registrar))
                registrar.assert_not_called()
        for motivo in ("In Review parada", "In Review parada há 12 h com conta ociosa (KRN-1600) causa nova"):
            registrar = mock.Mock()
            tipo = TK.tipo_execucao(motivo)
            self.assertEqual("outro", tipo)
            retomada.exigir_tipo(tipo, "linear-krn-1528", registrar=registrar)
            registrar.assert_called_once()


    def test_inc_316_treino_f4_bru_1097(self):
        # uso.jsonl: 2026-10-22T04:08:08+00:00 a 04:09:20, motivo operacional explícito.
        motivo = "treino F4 previsto para 14h BRT"
        registrar = mock.Mock()
        tipo = TK.tipo_execucao(motivo, {"identifier": "KRN-1097"})
        self.assertEqual("manual", tipo)
        self.assertEqual("manual", retomada.exigir_tipo(tipo, "linear-krn-1097", registrar=registrar))
        registrar.assert_not_called()
        for desconhecido in (motivo + " com causa nova", "treino desconhecido", "treino F4 previsto"):
            with self.subTest(motivo=desconhecido):
                registrar = mock.Mock()
                tipo = TK.tipo_execucao(desconhecido)
                self.assertEqual("outro", tipo)
                retomada.exigir_tipo(tipo, "linear-krn-1097", registrar=registrar)
                registrar.assert_called_once()
                self.assertEqual("execucao_sem_tipo", registrar.call_args.args[1])

    def test_inc_267_primeira_execucao_bru_1503(self):
        # uso.jsonl: início 2026-10-20T19:57:22+00:00, motivo_volta="primeira".
        registrar = mock.Mock()
        tipo = TK.tipo_execucao("primeira", {"identifier": "KRN-1503"})
        self.assertEqual("primeira", tipo)
        self.assertEqual("primeira", retomada.exigir_tipo(tipo, "linear-krn-1503", registrar=registrar))
        registrar.assert_not_called()
        for motivo in ("primeiramente", "primeira com causa desconhecida", "motivo desconhecido"):
            with self.subTest(motivo=motivo):
                registrar = mock.Mock()
                tipo = TK.tipo_execucao(motivo)
                self.assertEqual("outro", tipo)
                retomada.exigir_tipo(tipo, "linear-krn-1503", registrar=registrar)
                registrar.assert_called_once()
                self.assertEqual("execucao_sem_tipo", registrar.call_args.args[1])

    def test_inc_210_fora_do_fantasma_oauth(self):
        # uso.jsonl:, fim 2026-10-18T20:53:20+00:00.
        motivo = "orquestrador: fora do fantasma OAuth"
        registrar = mock.Mock()
        tipo = TK.tipo_execucao(motivo, {"identifier": "KRN-1215"})
        self.assertEqual("manual", tipo)
        self.assertEqual("manual", retomada.exigir_tipo(tipo, "linear-krn-1215", registrar=registrar))
        registrar.assert_not_called()
        for desconhecido in (motivo + " com causa nova", "orquestrador: fora do fantasma"):
            with self.subTest(motivo=desconhecido):
                registrar = mock.Mock()
                tipo = TK.tipo_execucao(desconhecido)
                self.assertEqual("outro", tipo)
                retomada.exigir_tipo(tipo, "linear-krn-1215", registrar=registrar)
                registrar.assert_called_once()
                self.assertEqual("execucao_sem_tipo", registrar.call_args.args[1])  # fora de `outra`

    def test_inc_207_reenvio_apos_oauth_de_outra_instalacao(self):
        # uso.jsonl: linear-, encerrada em 2026-10-18T18:35:54+00:00.
        motivo = "orquestrador: caiu por OAuth de outra instalação (0 min); fila local sem sessão"
        for minutos in (0, 12):
            with self.subTest(minutos=minutos):
                registrar = mock.Mock()
                tipo = TK.tipo_execucao(motivo.replace("(0 min)", f"({minutos} min)"),
                                        {"identifier": "KRN-1170"})
                self.assertEqual("manual", tipo)
                self.assertEqual("manual", retomada.exigir_tipo(tipo, "linear-krn-1170", registrar=registrar))
                registrar.assert_not_called()
        for desconhecido in ("orquestrador: caiu por OAuth de outra instalação",
                             motivo + " com causa nova", "orquestrador: motivo desconhecido"):
            with self.subTest(desconhecido=desconhecido):
                registrar = mock.Mock()
                tipo = TK.tipo_execucao(desconhecido)
                self.assertEqual("outro", tipo)
                retomada.exigir_tipo(tipo, "linear-krn-1170", registrar=registrar)
                registrar.assert_called_once()

    def test_inc_202_registro_real_bru_1009_nao_abre_incidente(self):
        registro = {
            "nome": "linear-krn-1009",
            "inicio": "2026-10-18T15:57:32+00:00",
            "fim": "2026-10-18T16:06:10+00:00",
            "motivo_volta": "orquestrador: reenfileirada direto (falha OAuth do fantasma, sem sessão)",
        }
        registrar = mock.Mock()
        tipo = TK.tipo_execucao(registro["motivo_volta"], {"identifier": "KRN-1009"})
        self.assertEqual("manual", tipo)
        self.assertEqual("manual", retomada.exigir_tipo(tipo, registro["nome"], registrar=registrar))
        registrar.assert_not_called()
        # Motivos desconhecidos continuam abrindo incidente, inclusive palavras maiores.
        for motivo in ("orquestrador: outro motivo", "orquestrador: reenfileirada diretamente"):
            with self.subTest(motivo=motivo):
                registrar = mock.Mock()
                tipo = TK.tipo_execucao(motivo)
                self.assertEqual("outro", tipo)
                retomada.exigir_tipo(tipo, registro["nome"], registrar=registrar)
                registrar.assert_called_once()

    def test_inc_201_registro_real_bru_1196_nao_abre_incidente(self):
        registro = {
            "nome": "linear-krn-1196",
            "inicio": "2026-10-18T14:57:25+00:00",
            "fim": "2026-10-18T15:28:03+00:00",
            "motivo_volta": "orquestrador: reenfileirada direto (falha OAuth do fantasma, sem sessão)",
        }
        registrar = mock.Mock()
        tipo = TK.tipo_execucao(registro["motivo_volta"], {"identifier": "KRN-1196"})
        self.assertEqual("manual", tipo)
        self.assertEqual("manual", retomada.exigir_tipo(tipo, registro["nome"], registrar=registrar))
        registrar.assert_not_called()
        # Motivos desconhecidos continuam abrindo incidente, inclusive palavras maiores.
        for motivo in ("orquestrador: outro motivo", "orquestrador: reenfileirada diretamente"):
            with self.subTest(motivo=motivo):
                registrar = mock.Mock()
                tipo = TK.tipo_execucao(motivo)
                self.assertEqual("outro", tipo)
                retomada.exigir_tipo(tipo, registro["nome"], registrar=registrar)
                registrar.assert_called_once()

    def test_inc_200_proposta_aprovada_bru_1199_nao_abre_incidente(self):
        # uso.jsonl: execução encerrada em 2026-10-18T15:04:09+00:00.
        for motivo in ("orquestrador: proposta aprovada", "orquestrador: proposta aprovada (pedido 123)"):
            with self.subTest(motivo=motivo):
                registrar = mock.Mock()
                tipo = TK.tipo_execucao(motivo, {"identifier": "KRN-1199"})
                self.assertEqual("proposta", tipo)
                self.assertEqual("proposta", retomada.exigir_tipo(tipo, "linear-krn-1199", registrar=registrar))
                registrar.assert_not_called()
        for motivo in ("orquestrador: proposta aprovadamente", "orquestrador: motivo desconhecido"):
            registrar = mock.Mock()
            tipo = TK.tipo_execucao(motivo)
            self.assertEqual("outro", tipo)
            retomada.exigir_tipo(tipo, "linear-krn-1199", registrar=registrar)
            registrar.assert_called_once()

    def test_inc_198_reenvio_do_orquestrador_tem_tipo_sem_incidente(self):
        for motivo in ("orquestrador: reenfileirada direto",
                       "orquestrador: reenfileirada direto (falha OAuth do fantasma, sem sessão)"):
            with self.subTest(motivo=motivo):
                registrar = mock.Mock()
                tipo = TK.tipo_execucao(motivo, ITEM)
                self.assertEqual("manual", tipo)
                self.assertEqual("manual", retomada.exigir_tipo(tipo, "linear-krn-1197", registrar=registrar))
                registrar.assert_not_called()
        # Uma mensagem nova do orquestrador continua exigindo classificação explícita.
        self.assertEqual("outro", TK.tipo_execucao("orquestrador: motivo desconhecido", ITEM))

    def test_inc_199_registro_real_bru_1198_nao_abre_incidente(self):
        # uso.jsonl: execução encerrada em 2026-10-18T14:55:21+00:00.
        motivo = "orquestrador: reenfileirada direto (falha OAuth do fantasma, sem sessão)"
        registrar = mock.Mock()
        tipo = TK.tipo_execucao(motivo, {"identifier": "KRN-1198"})
        self.assertEqual("manual", tipo)
        self.assertEqual("manual", retomada.exigir_tipo(tipo, "linear-krn-1198", registrar=registrar))
        registrar.assert_not_called()
        # O prefixo operacional não pode engolir motivos novos ou palavras maiores.
        for desconhecido in ("orquestrador: outro motivo", "orquestrador: reenfileirada diretamente"):
            self.assertEqual("outro", TK.tipo_execucao(desconhecido))

    def test_tipo_sempre_do_conjunto_conhecido(self):
        for motivo in (None, "", "   ", "motivo novo que ninguém previu", "voltou da espera do CI (x; vermelho)", "manual",
                       "linear KRN-1", "volta: conferência", "voltou de Done sem recibo", "alarme de tarefa parada"):
            for item in (None, {"comentarios": []}, {"comentarios": ["Claude: oi"]}):
                self.assertIn(TK.tipo_execucao(motivo, item), TK.TIPOS, (motivo, item))
                self.assertTrue(TK.tipo_execucao(motivo, item))

    def test_causa_no_motivo_nao_muda_o_tipo(self):
        for causa in ("vermelho", "sem_revisao", "revisao_com_achado", "vencida", "mecanica"):
            self.assertEqual(TK.tipo_execucao(f"voltou da espera do CI ({PR}; {causa})"), "espera_ci")

    def test_todo_motivo_de_enfileirar_no_codigo_tem_tipo_proprio(self):
        """Quem inventa um motivo novo em `enfileirar(...)` sem casar um tipo cai em "outro": o teste quebra."""
        achados = set()
        for arq in [*Path(agenda.__file__).parent.glob("*.py"),
                    *(Path(__file__).parent / "fixtures" / "tarefas").glob("*.md")]:
            for m in re.finditer(r"enfileirar\(\s*[\w.\[\]\"']+,\s*f?\"([^\"{]+)", arq.read_text(encoding="utf-8")):
                achados.add(m.group(1))
        self.assertTrue(achados)
        sem_tipo = {m for m in achados if TK.tipo_execucao(m + "X" if m.endswith(" ") else m, {"comentarios": []}) == "outro"}
        self.assertEqual(set(), sem_tipo, "motivo de fila sem tipo em tokens._TIPOS")

    def test_revisor_devolve_correcao_com_tipo_e_sem_incidente(self):
        # INC-152/181/182/184/185/186: a devolução do revisor caía em "outro"; com sufixo de causa também vale.
        for motivo in ("codex-revisor-merge: correção necessária",
                       "codex-revisor-merge: correção necessária (CI vermelho)",
                       "codex-revisor-merge: correção necessária\n"):
            with self.subTest(motivo=motivo):
                registrar = mock.Mock()
                tipo = TK.tipo_execucao(motivo, ITEM)
                self.assertEqual("conferencia", tipo)
                self.assertEqual("conferencia", retomada.exigir_tipo(tipo, NOME, registrar=registrar))
                registrar.assert_not_called()

    def test_exigir_tipo_valido_passa_e_vazio_vira_outro_com_incidente(self):
        incidentes = []

        def registrar(falha, classe, peca, issue="", **k):
            incidentes.append((falha, classe, issue, k.get("chave")))
        self.assertEqual("espera_ci", retomada.exigir_tipo("espera_ci", NOME, registrar=registrar))
        self.assertEqual([], incidentes)
        for vazio in (None, "", "?", "inventado", "outro"):
            self.assertEqual("outro", retomada.exigir_tipo(vazio, NOME, agora=AGORA, registrar=registrar))
        self.assertEqual(5, len(incidentes))
        self.assertEqual("KRN-77", incidentes[0][2])
        self.assertIn("sem tipo", incidentes[0][0])

    def test_falha_no_incidente_nao_derruba_o_registro(self):
        def quebra(*a, **k):
            raise OSError("disco")
        self.assertEqual("outro", retomada.exigir_tipo(None, NOME, registrar=quebra))


class RegistroSempreComTipoTest(DadosTest):
    def test_registro_sem_tipo_recebe_o_da_origem_e_nunca_fica_vazio(self):
        for origem, esperado in (("hub", "agendada"), ("agente", "manual"), ("tarefa", "agendada"), ("sei-la", "outro"), (None, "outro")):
            conta.registrar({"inicio": conta.iso(AGORA), "nome": "x", "origem": origem}, self.dados)
        conta.registrar({"inicio": conta.iso(AGORA), "nome": "y", "origem": "tarefa", "tipo": "espera_ci"}, self.dados)
        regs = conta.ler_uso(None, self.dados)
        self.assertEqual(["agendada", "manual", "agendada", "outro", "outro", "espera_ci"], [r["tipo"] for r in regs])
        self.assertTrue(all(r["tipo"] in TK.TIPOS for r in regs))


class TetoTest(DadosTest):
    def test_abaixo_de_10_nao_pausa(self):
        self.registrar(9)
        self.assertIsNone(retomada.teto(NOME, None, self.dados))

    def test_limite_e_sinal_nao_contam(self):
        self.registrar(8)
        conta.registrar(uso(NOME, 1, limite=True), self.dados)
        conta.registrar(uso(NOME, 2, interrompida=True), self.dados)
        self.assertEqual(8, len(retomada.execucoes(NOME, None, self.dados)))

    def test_10_pausa_e_fala_nova_recomeca_a_contagem(self):
        self.registrar(10)
        self.assertIn("rodou 10 execuções desde a última fala", retomada.teto(NOME, None, self.dados))
        fala = conta.iso(AGORA - dt.timedelta(minutes=5))  # depois da maioria das execuções
        self.assertIsNone(retomada.teto(NOME, fala, self.dados))
        self.assertIn("rodou 10", retomada.teto(NOME, conta.iso(AGORA - dt.timedelta(days=1)), self.dados))

    def test_aviso_aos_6_uma_vez_por_janela(self):
        avisos = []
        self.registrar(5)
        self.assertFalse(retomada.avisar_seis(ITEM, self.dados, AGORA, lambda i, t: avisos.append(t)))
        conta.registrar(uso(NOME, 1), self.dados)
        self.assertTrue(retomada.avisar_seis(ITEM, self.dados, AGORA, lambda i, t: avisos.append(t)))
        self.assertFalse(retomada.avisar_seis(ITEM, self.dados, AGORA, lambda i, t: avisos.append(t)))
        self.assertEqual(1, len(avisos))
        self.assertIn("6 execuções", avisos[0])
        self.assertIn("espera_ci 6", avisos[0])
        # fala nova: janela nova; 6 de novo avisa de novo
        item = {**ITEM, "ultima_fala": conta.iso(AGORA + dt.timedelta(minutes=1))}
        with mock.patch("hub.conta.agora", lambda: AGORA + dt.timedelta(hours=1)):
            for i in range(6):
                conta.registrar({**uso(NOME, 0), "inicio": conta.iso(AGORA + dt.timedelta(minutes=2 + i))}, self.dados)
            self.assertTrue(retomada.avisar_seis(item, self.dados, AGORA, lambda i, t: avisos.append(t)))

    def test_pausa_chama_o_orquestrador_com_os_numeros_e_comenta_uma_vez(self):
        self.registrar(10, custo=2.5)
        chamados, comentarios = [], []
        freio = retomada.teto(NOME, None, self.dados)
        for _ in range(2):
            retomada.pausar(ITEM, freio, self.dados, AGORA, lambda *a: chamados.append(a), lambda i, t: comentarios.append(t))
        self.assertEqual(2, len(chamados))  # o dedupe de 6 h é de quem passa o `chamar_orq` (agenda)
        ident, motivo, origem = chamados[0]
        self.assertEqual(("KRN-77", "teto de execuções"), (ident, origem))
        self.assertIn("10 execuções (espera_ci 10)", motivo)
        self.assertIn("US$ 25.00", motivo)
        self.assertEqual(1, len(comentarios))
        self.assertIn("Pausei a tarefa", comentarios[0])

    def test_enfileirar_bloqueia_no_teto_chama_orq_e_libera_com_fala(self):
        t = linear.tarefa({"identifier": "KRN-77", "id": "uuid-77", "tipo": "issue", "titulo": "x", "url": "u",
                           "descricao": "d", "comentarios": [], "repos": [], "modelo": "opus"})
        self.registrar(10)
        with mock.patch.object(agenda, "_chamar_orq") as orch, mock.patch.object(linear, "comentar") as com, \
                mock.patch.object(agenda, "em_loop", lambda *a, **k: None):
            self.assertFalse(agenda.enfileirar(t, "proposta feita", self.dados, substituir=True))
            self.assertFalse(agenda.enfileirar(t, "voltou da espera do CI (x)", self.dados, freio_conferido=True))
            self.assertEqual([], agenda.fila(self.dados))
            orch.assert_called_once()  # dedupe de 6 h
            self.assertIn("teto de 10 execuções", orch.call_args[0][1])
            com.assert_called_once()
            t.linear["ultima_fala"] = conta.iso(AGORA - dt.timedelta(minutes=1))  # o orquestrador decidiu
            self.assertTrue(agenda.enfileirar(t, "linear KRN-77", self.dados))

    def test_executar_registra_tipo_e_aviso_aos_6(self):
        """O ponto único de registro da execução: nunca grava tipo vazio/"outro" (incidente) e avisa aos 6."""
        def rodar(motivo, incidentes):
            t = linear.tarefa({"identifier": "KRN-78", "id": "uuid-78", "tipo": "issue", "titulo": "x", "url": "u",
                               "descricao": "d", "comentarios": [], "repos": [], "modelo": "sonnet"})
            t.motivo = motivo
            with mock.patch.object(conta, "agora", lambda: AGORA), \
                 mock.patch.object(agenda, "montar_prompt", lambda t, base_dados=None: "prompt"), \
                 mock.patch("hub.incidentes.registrar", lambda *a, **k: incidentes.append(a[0])), \
                 mock.patch.object(retomada, "avisar_seis") as aviso:
                reg = agenda.executar(t, base_dados=self.dados, saida_dir=os.path.join(self.dados, "saida"),
                                      chamar=lambda *a: (0, {"result": "ok"}, ""), avisar_fn=lambda *a, **k: None)
            return reg, aviso
        incidentes = []
        reg, aviso = rodar("voltou da espera do CI (x)", incidentes)
        self.assertEqual("espera_ci", reg["tipo"])
        self.assertEqual([], incidentes)
        aviso.assert_called_once()
        reg, _ = rodar("motivo que ninguém classificou", incidentes)
        self.assertEqual("outro", reg["tipo"])
        self.assertEqual(1, len(incidentes))
        self.assertIn("sem tipo", incidentes[0])


class GanchoMecanicoTest(DadosTest):
    def gravar_falso(self, gravadas):
        def gravar(ident, pr, **k):
            gravadas.append((ident, pr, k.get("no_ar")))
            return {"ident": ident, "pr": pr}
        return gravar

    def test_pr_aberto_grava_espera_de_ci(self):
        gravadas = []
        gh = lambda c: {"state": "open", "merged": False}  # noqa: E731
        r = retomada.gancho_mecanico("KRN-77", f"Terminei. PR {PR} no CI, aguardo.", self.gravar_falso(gravadas), gh,
                                     self.dados, AGORA)
        self.assertTrue(r)
        self.assertEqual([("KRN-77", PR, None)], gravadas)

    def test_pr_fundido_recente_grava_espera_de_deploy(self):
        gravadas = []
        gh = lambda c: {"state": "closed", "merged": True, "merged_at": conta.iso(AGORA - dt.timedelta(hours=1))}  # noqa: E731
        self.assertTrue(retomada.gancho_mecanico("KRN-77", PR, self.gravar_falso(gravadas), gh, self.dados, AGORA))
        self.assertEqual([("KRN-77", PR, "")], gravadas)

    def test_fundido_antigo_fechado_ou_sem_pr_nao_serve(self):
        gravadas = []
        antigo = lambda c: {"merged": True, "merged_at": conta.iso(AGORA - dt.timedelta(days=30))}  # noqa: E731
        fechado = lambda c: {"state": "closed", "merged": False}  # noqa: E731
        self.assertIsNone(retomada.gancho_mecanico("KRN-77", PR, self.gravar_falso(gravadas), antigo, self.dados, AGORA))
        self.assertIsNone(retomada.gancho_mecanico("KRN-77", PR, self.gravar_falso(gravadas), fechado, self.dados, AGORA))
        self.assertIsNone(retomada.gancho_mecanico("KRN-77", "sem link", self.gravar_falso(gravadas), fechado, self.dados, AGORA))
        self.assertEqual([], gravadas)

    def test_promessa_com_pr_nao_volta_para_a_fila(self):
        """_concluir: "aguardo" com PR citado vira `voltar-quando` por código, sem refazer a sessão."""
        with tempfile.TemporaryDirectory() as d:
            saida = Path(d, "r.md")
            saida.write_text(f"O agente está rodando, PR {PR}. Aguardo.", encoding="utf-8")
            item = {"tipo": "issue", "id": "u", "identifier": "KRN-77", "titulo": "t", "descricao": "", "url": "x",
                    "modelo": "sonnet", "repos": [], "comentarios": [], "estados": {"In Review": "r", "In Progress": "p"}}
            reg = {"ok": True, "saida": str(saida), "conta": "1", "duracao_s": 60, "inicio": "2026-10-17T11:00:00+00:00"}
            with mock.patch.object(linear, "gql", side_effect=OSError("x")), mock.patch.object(linear, "atividade"), \
                    mock.patch.object(linear, "_mover_no_fim") as mover, mock.patch.object(linear, "avisar_zap", lambda *a, **k: None), \
                    mock.patch.object(retomada, "gancho_mecanico", return_value={"pr": PR}) as gancho:
                volta = linear._concluir(linear.tarefa(item), reg, conferir_fn=lambda i, t: "")
        self.assertIsNone(volta)
        gancho.assert_called_once()
        self.assertEqual("In Progress", mover.call_args[0][1])  # a issue espera em In Progress
        linhas = [json.loads(x) for x in Path(self.dados, "claude", "sem-ia.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual("promessa", linhas[-1]["tipo"])


class ConferenciaMecanicaTest(DadosTest):
    def r(self, **k):
        return retomada.conferir_mecanica(ITEM, "texto", issue={"identifier": "KRN-77"}, avaliar=lambda i, comentario=None: k,
                                          gancho=lambda ident, texto: {"pr": PR})

    def test_confere_tudo_ok(self):
        self.assertEqual({"resultado": "ok", "faltas": []}, self.r(ok=True, faltas=[]))

    def test_deploy_subindo_grava_espera_sem_ia(self):
        self.assertEqual("esperar", self.r(ok=False, esperar=True, faltas=[])["resultado"])

    def test_falha_traz_o_motivo_pronto(self):
        r = self.r(ok=False, esperar=False, faltas=["o PR x#1 ainda está aberto (não mergeado)"])
        self.assertEqual(("falha", ["o PR x#1 ainda está aberto (não mergeado)"]), (r["resultado"], r["faltas"]))

    def test_erro_de_leitura_volta_ao_caminho_de_sempre(self):
        def quebra(*a, **k):
            raise OSError("sem rede")
        self.assertEqual("indisponivel", retomada.conferir_mecanica(ITEM, "t", issue={}, avaliar=quebra)["resultado"])

    def test_conferir_marca_mecanica_so_quando_o_modelo_diz(self):
        extra = {}
        resp = '{"completo": false, "faltou": "confirmar que o PR está no ar", "mecanica": true}'
        self.assertEqual("confirmar que o PR está no ar", linear.conferir({"titulo": "t"}, "x", chamar=lambda p: resp, extra=extra))
        self.assertTrue(extra["mecanica"])
        extra = {}
        linear.conferir({"titulo": "t"}, "x", chamar=lambda p: '{"completo": false, "faltou": "anexar o PDF"}', extra=extra)
        self.assertFalse(extra["mecanica"])
        extra = {}
        linear.conferir({"titulo": "t"}, "x", chamar=lambda p: '{"completo": true, "mecanica": true}', extra=extra)
        self.assertFalse(extra["mecanica"])

    def concluir(self, extra_mecanica, resultado, tipo=None):
        with tempfile.TemporaryDirectory() as d:
            saida = Path(d, "r.md")
            saida.write_text("Entreguei; falta só conferir se subiu.", encoding="utf-8")
            item = {"tipo": "issue", "id": "u", "identifier": "KRN-77", "titulo": "t", "descricao": "", "url": "x",
                    "modelo": "sonnet", "repos": [], "comentarios": [], "estados": {"In Review": "r", "In Progress": "p"}}
            reg = {"ok": True, "saida": str(saida), "conta": "1", "duracao_s": 60, "inicio": "2026-10-17T11:00:00+00:00"}
            if tipo:
                reg["tipo"] = tipo
            self.chamou_conferir = False

            def conferir(i, t, extra=None, **k):
                self.chamou_conferir = True
                extra["mecanica"] = extra_mecanica
                return "conferir que está no ar"
            with mock.patch.object(linear, "gql", side_effect=OSError("x")), mock.patch.object(linear, "atividade"), \
                    mock.patch.object(linear, "_mover_no_fim") as mover, mock.patch.object(linear, "avisar_zap", lambda *a, **k: None), \
                    mock.patch.object(linear, "conferir", conferir), \
                    mock.patch.object(retomada, "conferir_mecanica", return_value=resultado) as mec:
                volta = linear._concluir(linear.tarefa(item), reg)
        return volta, mover, mec

    def test_pergunta_de_codigo_que_confere_fecha_sem_sessao(self):
        volta, mover, mec = self.concluir(True, {"resultado": "ok", "faltas": []})
        self.assertIsNone(volta)
        self.assertEqual("In Review", mover.call_args[0][1])
        linhas = Path(self.dados, "claude", "sem-ia.jsonl").read_text(encoding="utf-8")
        self.assertIn('"conferencia"', linhas)

    def test_pergunta_de_codigo_que_falha_volta_com_o_motivo_pronto(self):
        volta, _, _ = self.concluir(True, {"resultado": "falha", "faltas": ["https://x.com respondeu 404"]})
        self.assertIn("faltou https://x.com respondeu 404", volta.linear["comentarios"][-1])

    def test_pergunta_de_codigo_com_deploy_subindo_espera_em_in_progress(self):
        volta, mover, _ = self.concluir(True, {"resultado": "esperar", "faltas": []})
        self.assertIsNone(volta)
        self.assertEqual("In Progress", mover.call_args[0][1])

    def test_falta_de_trabalho_volta_como_sempre(self):
        volta, _, mec = self.concluir(False, {"resultado": "ok", "faltas": []})
        mec.assert_not_called()
        self.assertIn("faltou conferir que está no ar", volta.linear["comentarios"][-1])

    def test_leitura_indisponivel_volta_como_sempre(self):
        volta, _, _ = self.concluir(True, {"resultado": "indisponivel", "faltas": []})
        self.assertIn("faltou conferir que está no ar", volta.linear["comentarios"][-1])

    def test_retomada_de_espera_confere_pelo_recibo_sem_abrir_sessao(self):
        # 19-21/10: 179 das 362 conferências vieram logo depois de espera_deploy, espera_ci, parada ou recibo
        for tipo in sorted(retomada.SEM_CONFERENCIA_APOS):
            volta, mover, mec = self.concluir(False, {"resultado": "ok", "faltas": []}, tipo=tipo)
            self.assertFalse(self.chamou_conferir, tipo)
            mec.assert_called_once()
            self.assertIsNone(volta, tipo)
            self.assertEqual("In Review", mover.call_args[0][1], tipo)

    def test_retomada_de_espera_com_recibo_falhando_volta_com_o_motivo(self):
        # Codex P2 (PR #836): nunca fechar sem nenhuma conferência, nem depois de mudança de código na retomada
        for tipo in sorted(retomada.SEM_CONFERENCIA_APOS):
            volta, _, _ = self.concluir(False, {"resultado": "falha", "faltas": ["PR não entrou"]}, tipo=tipo)
            self.assertFalse(self.chamou_conferir, tipo)
            self.assertIn("faltou PR não entrou", volta.linear["comentarios"][-1], tipo)

    def test_retomada_de_espera_com_recibo_indisponivel_cai_na_conferencia_de_sempre(self):
        for tipo in sorted(retomada.SEM_CONFERENCIA_APOS):
            volta, _, _ = self.concluir(False, {"resultado": "indisponivel", "faltas": []}, tipo=tipo)
            self.assertTrue(self.chamou_conferir, tipo)
            self.assertIn("faltou conferir que está no ar", volta.linear["comentarios"][-1], tipo)

    def test_primeira_fala_e_proposta_continuam_conferidas(self):
        for tipo in ("primeira", "fala", "proposta", "fala_durante", "promessa", None):
            volta, _, _ = self.concluir(False, {"resultado": "ok", "faltas": []}, tipo=tipo)
            self.assertTrue(self.chamou_conferir, tipo)
            self.assertIn("faltou conferir que está no ar", volta.linear["comentarios"][-1], tipo)

    def test_so_tipos_de_retomada_de_espera_pulam_a_conferencia(self):
        self.assertLessEqual(retomada.SEM_CONFERENCIA_APOS, TK.TIPOS_RETOMADA)
        self.assertFalse(retomada.pular_conferencia("fala"))
        self.assertFalse(retomada.pular_conferencia(None))


class ParadaSemSessaoTest(DadosTest):
    def test_parada_com_pr_citado_grava_espera_em_vez_de_reabrir(self):
        issue = {"identifier": "KRN-77", "id": "uuid", "comments": {"nodes": [
            {"createdAt": "2026-10-17T08:00:00Z", "body": f"PR {PR} aberto, CI rodando.", "user": {"app": True}}]},
            "team": {"id": "t", "states": {"nodes": []}}}
        gravadas = []
        with mock.patch.object(retomada, "gancho_mecanico", lambda ident, texto, **k: gravadas.append((ident, texto)) or {"pr": PR}), \
                mock.patch.object(linear, "reabrir") as reabrir, mock.patch.object(linear, "item_por_ident",
                                                                                    return_value={"ultima_fala": None, "estado_desde": None}), \
                mock.patch.object(agenda, "em_loop", lambda *a, **k: None):
            voltar = parada.vigiar(self.dados, AGORA, consulta=lambda q, **v: {"issues": {"nodes": [issue]}}, trabalho=self.dados,
                                   log=lambda *a: None)
        self.assertEqual([], voltar)
        reabrir.assert_not_called()
        self.assertEqual("KRN-77", gravadas[0][0])
        linhas = Path(self.dados, "claude", "sem-ia.jsonl").read_text(encoding="utf-8")
        self.assertIn('"parada"', linhas)


class EsperaCiTest(DadosTest):
    def test_revisao_so_com_p3_e_limpa_mas_p0_p1_p2_nao(self):
        def rev(corpo):
            return {"body": f"**Revisão do Codex** (commit `{SHA[:8]}`)\n\n{corpo}"}
        self.assertTrue(esperas.revisao_limpa(rev("Sem achados sérios."), SHA))
        self.assertTrue(esperas.revisao_limpa(rev("[P3] nome da variável poderia ser mais claro."), SHA))
        for grave in ("[P0] quebra tudo", "[P1] bug", "[P2] regressão no caso vazio", "[P3] nit\n[P2] bug"):
            self.assertFalse(esperas.revisao_limpa(rev(grave), SHA), grave)
        self.assertFalse(esperas.revisao_limpa(rev("[P3] nit. Revisão parcial: diff cortado."), SHA))
        self.assertFalse(esperas.revisao_limpa(rev("Achei coisas, sem etiqueta."), SHA))

    def causa(self, gh, minutos):
        e = esperas.gravar("krn-164", PR, 6, chamar=GitHubFalso(), base_dados=self.dados, agora=T0)
        out = {}
        fala = esperas.situacao(e, gh, T0 + dt.timedelta(minutes=minutos), _out=out)
        return fala, out

    def test_causa_do_despertar(self):
        achado = {"body": f"**Revisão do Codex** (commit `{SHA[:8]}`)\n\n[P1] quebra x.", "created_at": "2026-10-12T12:30:00Z"}
        casos = [(GitHubFalso(ci("failure"), [achado]), 35, "vermelho"),
                 (GitHubFalso(ci("success"), [achado]), 35, "revisao_com_achado"),
                 (GitHubFalso(ci("success"), []), 150, "sem_revisao")]
        for gh, minutos, causa in casos:
            fala, out = self.causa(gh, minutos)
            self.assertTrue(fala, causa)
            self.assertEqual(causa, out.get("causa"))

    def test_ainda_rodando_nao_acorda_nem_tem_causa(self):
        fala, out = self.causa(GitHubFalso(ci("pending"), []), 40)
        self.assertIsNone(fala)
        self.assertNotIn("causa", out)

    def test_causa_vai_no_motivo_da_fila_e_o_tipo_segue_espera_ci(self):
        esperas.gravar("krn-164", PR, 6, chamar=GitHubFalso(), base_dados=self.dados, agora=T0)
        achado = {"body": f"**Revisão do Codex** (commit `{SHA[:8]}`)\n\n[P1] quebra x.", "created_at": "2026-10-12T12:30:00Z"}

        def reabrir(ident, fala):
            return linear.tarefa({"tipo": "issue", "id": "u", "identifier": ident, "titulo": "t", "descricao": "", "url": "x",
                                  "modelo": "sonnet", "repos": [], "comentarios": [fala], "estados": {}})
        with mock.patch.object(linear, "reabrir", reabrir), mock.patch.object(agenda, "em_loop", lambda *a, **k: None), \
                mock.patch.object(linear, "gql", side_effect=AssertionError("Linear")):
            agenda.voltar_das_esperas({}, self.dados, chamar=GitHubFalso(ci("failure"), [achado]),
                                      agora=T0 + dt.timedelta(minutes=35))
        item = agenda.fila(self.dados)[0]
        self.assertIn("; vermelho)", item["motivo"])
        self.assertEqual("espera_ci", TK.tipo_execucao(item["motivo"], item["linear"]))

    def causa_deploy(self, pr, minutos, recibo_no_ar=None, tem_site=True):
        e = esperas.gravar("krn-713", PR, 6, chamar=GitHubFalso(), base_dados=self.dados, agora=T0, no_ar="")
        out = {}
        with mock.patch("hub.recibo.no_ar", return_value=recibo_no_ar or (False, "ainda não")), \
                mock.patch.object(esperas, "_tem_site", lambda r: tem_site):
            fala = esperas.situacao(e, GitHubFalso(pr=pr), T0 + dt.timedelta(minutes=minutos), _out=out)
        return fala, out

    def test_causa_de_cada_abertura_da_espera_do_deploy(self):
        fundido = {"state": "closed", "merged": True, "merge_commit_sha": SHA, "head": {"sha": SHA}}
        casos = [({"state": "closed", "merged": False, "head": {"sha": SHA}}, 10, None, "pr_fechado"),
                 ({"state": "open", "merged": False, "head": {"sha": SHA}}, 6 * 60 + 1, None, "pr_sem_merge"),
                 (fundido, 6 * 60 + 1, None, "deploy_vencido"),
                 (fundido, 10, (None, "o deploy deste repo não tem como conferir"), "deploy_nao_confere")]
        for pr, minutos, recibo, causa in casos:
            fala, out = self.causa_deploy(pr, minutos, recibo)
            self.assertTrue(fala, causa)
            self.assertEqual(causa, out.get("causa"))

    def test_deploy_ainda_subindo_nao_acorda_nem_tem_causa(self):
        fundido = {"state": "closed", "merged": True, "merge_commit_sha": SHA, "head": {"sha": SHA}}
        fala, out = self.causa_deploy(fundido, 10)
        self.assertIsNone(fala)
        self.assertNotIn("causa", out)

    def test_causa_da_sessao_sem_ia_que_falha_vai_no_motivo_do_deploy(self):
        esperas.gravar("krn-164", PR, 6, chamar=GitHubFalso(), base_dados=self.dados, agora=T0, no_ar="")
        fundido = {"state": "closed", "merged": True, "merge_commit_sha": SHA, "head": {"sha": SHA}}

        def reabrir(ident, fala):
            return linear.tarefa({"tipo": "issue", "id": "u", "identifier": ident, "titulo": "t", "descricao": "", "url": "x",
                                  "modelo": "sonnet", "repos": [], "comentarios": [fala], "estados": {}})

        def sem_ia(e, fala, d, *a, **k):
            d["causa_ia"] = "recibo_falta"
            return fala
        with mock.patch.object(linear, "reabrir", reabrir), mock.patch.object(agenda, "em_loop", lambda *a, **k: None), \
                mock.patch.object(agenda, "_sem_ia", sem_ia), mock.patch.object(linear, "gql", side_effect=AssertionError("L")), \
                mock.patch("hub.recibo.no_ar", return_value=(True, "no ar (abc)")), mock.patch("hub.telemetria.marcar_no_ar"):
            agenda.voltar_das_esperas({}, self.dados, chamar=GitHubFalso(pr=fundido), agora=T0 + dt.timedelta(minutes=10))
        item = agenda.fila(self.dados)[0]
        self.assertIn("; recibo_falta)", item["motivo"])

    def test_trecho_do_log_cai_para_o_job_vermelho_do_ci_yml(self):
        chamadas = []

        def chamar(caminho):
            chamadas.append(caminho)
            if "actions/runs?head_sha" in caminho:
                return {"workflow_runs": [{"id": 9, "conclusion": "success"}, {"id": 10, "conclusion": "failure"}]}
            return {"jobs": [{"id": 77, "conclusion": "success"}, {"id": 78, "conclusion": "failure"}]}
        logs = []

        def ler(caminho):
            logs.append(caminho)
            return "ok\n FAIL  tests/x.test.ts > grupo > nome\nAssertionError: 1 != 2\n"
        with mock.patch.object(esperas, "LOG_CI", "/nao/existe/{sha}.log"):
            trecho = esperas.trecho_log(SHA, "acme/clienteh", chamar, ler)
        self.assertEqual(["repos/acme/clienteh/actions/jobs/78/logs"], logs)
        self.assertIn("- tests/x.test.ts > grupo > nome", trecho)
        self.assertIn("job do ci.yml de acme/clienteh@39290728", trecho)

    def test_trecho_le_o_comentario_do_local_ci_antes_do_ci_yml(self):  #
        corpo = (f"<!-- local-ci-log {SHA} -->\n**local-ci vermelho** em `npm test` (12 min) no commit `{SHA[:8]}`, em vps-parceiro.\n\n"
                 "Testes que falharam:\n- tests/x.test.ts > grupo > nome\n- tests/y.test.ts > outro\n\n"
                 "Últimas 80 linhas do log:\n```\nlinha 1\nAssertionError: 1 != 2\n```\n")
        chamadas = []

        def chamar(caminho):
            chamadas.append(caminho)
            if caminho.endswith(f"commits/{SHA}/pulls?per_page=10"):
                return [{"number": 42}]
            if "issues/42/comments" in caminho:
                return [{"body": "revisão", "id": 1},
                        {"body": f"<!-- local-ci-log {'0' * 40} -->\nvelho\n```\nde outro sha\n```\n", "id": 2},
                        {"body": corpo, "id": 3, "updated_at": "2026-10-20T10:00:00Z"}]
            if "commits/" in caminho and "/comments" in caminho:
                return []
            raise AssertionError(f"foi ao ci.yml: {caminho}")
        with mock.patch.object(esperas, "LOG_CI", "/nao/existe/{sha}.log"):
            trecho = esperas.trecho_log(SHA, "acme/clienteh", chamar, lambda c: self.fail("leu o ci.yml"))
        self.assertIn("Testes que falharam:\n- tests/x.test.ts > grupo > nome\n- tests/y.test.ts > outro", trecho)
        self.assertIn("AssertionError: 1 != 2", trecho)
        self.assertIn("comentário do local-ci no PR #42", trecho)
        self.assertNotIn("de outro sha", trecho)

    def test_trecho_le_o_comentario_no_commit_da_main(self):  #
        corpo = f"<!-- local-ci-log {SHA} -->\nx\n\nÚltimas 80 linhas do log:\n```\nerro de build\n```\n"

        def chamar(caminho):
            if "/comments" in caminho and caminho.split("?")[0].endswith(f"commits/{SHA}/comments"):
                return [{"body": corpo}]
            return []
        with mock.patch.object(esperas, "LOG_CI", "/nao/existe/{sha}.log"):
            trecho = esperas.trecho_log(SHA, "acme/clienteh", chamar, lambda c: "")
        self.assertIn("erro de build", trecho)
        self.assertIn(f"comentário do local-ci no commit {SHA[:8]}", trecho)

    def test_trecho_sem_log_nem_job_mantem_o_aviso_de_nao_re_disparar(self):
        def chamar(caminho):
            raise OSError("sem rede")
        with mock.patch.object(esperas, "LOG_CI", "/nao/existe/{sha}.log"):
            trecho = esperas.trecho_log(SHA, "acme/clienteh", chamar, lambda c: "")
        self.assertIn("NÃO re-dispare às cegas", trecho)


class MedirTest(unittest.TestCase):
    def test_meta_por_porte_percentual_sem_ia_e_serie_diaria(self):
        usos = [uso("linear-krn-1", m, tipo="espera_ci") for m in (10, 20, 30)]  # pequena, 3 execuções
        usos += [uso("linear-krn-2", 10, tipo="primeira")]  # pequena, 1 execução
        usos += [uso("linear-krn-3", m, tipo="conferencia", custo=30) for m in (10, 20)]  # grande (US$ 60)
        usos += [uso("linear-krn-4", 5, tipo=None), uso("agenda-x", 5)]  # sem tipo; fora do Linear
        usos[-2].pop("tipo")
        sem_ia = [{"ident": "KRN-1", "tipo": "no_ar", "quando": conta.iso(AGORA - dt.timedelta(minutes=5))}] * 8
        r = retomada.medir(usos, sem_ia, AGORA, dias=2)
        self.assertEqual({"KRN-1": 3, "KRN-2": 1, "KRN-3": 2, "KRN-4": 1}, {t["issue"]: t["execucoes"] for t in r["por_tarefa"]})
        self.assertEqual(["KRN-3"], [t["issue"] for t in r["por_tarefa"] if t["grande"]])
        self.assertEqual(1.67, r["media_pequenas"])  # (3 + 1 + 1) / 3
        self.assertEqual(2.0, r["media_grandes"])
        self.assertEqual(1, r["sem_tipo"])
        self.assertEqual(6, r["retomadas_com_ia"])  # 3 espera_ci + 2 conferencia + 1 sem tipo
        self.assertEqual(8, r["retomadas_sem_ia"])
        self.assertEqual(57.1, r["pct_sem_ia"])
        self.assertEqual({"pequenas": True, "grandes": True, "sem_ia": False}, r["metas"])
        self.assertEqual(["KRN-1"], [t["issue"] for t in r["acima_da_meta"]])
        self.assertEqual({"espera_ci": 3, "primeira": 1, "conferencia": 2, "sem_tipo": 1, "sem_ia": 8}, r["diario"]["2026-10-17"])

    def test_janela_e_vazio(self):
        r = retomada.medir([uso("linear-krn-1", 60 * 24 * 5)], [], AGORA, dias=2)
        self.assertEqual([], r["por_tarefa"])
        self.assertEqual(0.0, r["pct_sem_ia"])

    def test_aba_retomadas_renderiza(self):
        usos = [uso("linear-krn-1", m) for m in (10, 20, 30)] + [uso("linear-krn-2", 10, tipo="primeira")]
        sem_ia = [{"ident": "KRN-1", "tipo": "no_ar", "quando": conta.iso(AGORA - dt.timedelta(minutes=5))}]
        html = painel._retomadas(usos, sem_ia, AGORA)
        for trecho in ("Retomadas sem IA", "meta: até 2", "meta: até 4", "Por dia e por tipo", "KRN-1", "Execuções sem tipo"):
            self.assertIn(trecho, html)

    def test_uso_tem_a_aba(self):
        with mock.patch.object(painel, "_retomadas", return_value="<p>x</p>"):
            self.assertIn("Retomadas", painel.uso("retomadas"))


if __name__ == "__main__":
    unittest.main()
