import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import propostas, raiz


class TimeoutAtualizarHubTest(unittest.TestCase):
    def test_cabe_a_suite_inteira(self):
        # a suíte leva ~900 s: com timeout de 900 o atualizar-hub estourava (código 124) e o hub ficava sem merges
        self.assertGreaterEqual(raiz.ACOES["atualizar-hub"][1], 1800)


class PropostasExecutaveisTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = tmp.name

    def criar(self, tipo, destino="x", conteudo="texto da proposta com tamanho suficiente para um post do google ok", dados=None):
        return propostas.criar("t", tipo, destino, "r", conteudo, dados=dados, base_dados=self.d)

    def test_outro_nao_existe_mais(self):
        with self.assertRaises(propostas.PropostaInvalida):
            self.criar("outro")

    def test_vps_exige_comandos_e_timeout(self):
        with self.assertRaises(propostas.PropostaInvalida):
            self.criar("vps", "VPS", "por quê", {})
        with self.assertRaises(propostas.PropostaInvalida):
            self.criar("vps", "VPS", "por quê", {"comandos": "docker image prune -f", "timeout_min": 99})
        p = self.criar("vps", "VPS", "por quê", {"comandos": "docker image prune -f"})
        self.assertEqual(p["como_executar"], "")

    def test_whatsapp_precisa_do_dono_do_numero(self):
        with self.assertRaises(propostas.PropostaInvalida):
            self.criar("whatsapp", "5500000000001", "oi", {"numero": "5500000000001"})
        with self.assertRaises(propostas.PropostaInvalida):
            self.criar("whatsapp", "5500000000001", "oi", {"remetente": "fsite06"})
        self.criar("whatsapp", "5500000000001", "oi", {"remetente": "fsite06", "contato_id": "0f8fad5b-d9cb-469f-a165-70867728950e"})
        self.criar("whatsapp", "5500000000001", "oi", {"remetente": "asite01", "numero": "5500000000001"})

    def test_gmb_e_avaliacao(self):
        with self.assertRaises(propostas.PropostaInvalida):
            self.criar("post_gmb", dados={"unidade": "centro"})
        with self.assertRaises(propostas.PropostaInvalida):
            self.criar("post_gmb", conteudo="texto com travessão — não pode, e com tamanho suficiente para passar",
                       dados={"unidade": "beta"})
        self.criar("post_gmb", dados={"unidade": "beta", "link": "https://site-b.exemplo.com/rpg"})
        with self.assertRaises(propostas.PropostaInvalida):
            self.criar("resposta_avaliacao", conteudo="Obrigado!", dados={"unidade": "beta"})
        self.criar("resposta_avaliacao", conteudo="Obrigado!", dados={"unidade": "beta", "review_id": "AbC_123-x"})


class PedidosSegurosTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)

    def test_pedir_valida_forma(self):
        with self.assertRaises(ValueError):
            raiz.pedir("rm-rf", base_dados=self.d)
        with self.assertRaises(ValueError):
            raiz.pedir("reiniciar-servico", base_dados=self.d)
        with self.assertRaises(ValueError):
            raiz.pedir("reiniciar-servico", "a; rm -rf /", base_dados=self.d)
        with self.assertRaises(ValueError):
            raiz.pedir("limpar-docker", "x", base_dados=self.d)
        pid = raiz.pedir("limpar-docker", motivo="disco 86%", base_dados=self.d)
        self.assertRegex(pid, raiz.RE_PID)

    def test_executa_e_grava_resultado(self):
        pid = raiz.pedir("limpar-docker", motivo="disco", base_dados=self.d)
        rodou = []
        regs = [raiz.executar_pedido(a, self.d, rodar=lambda cmd, t: (rodou.append(cmd), (0, "Total reclaimed space: 2GB"))[1])
                for a in sorted((self.d / "vps" / "pedidos").iterdir())]
        self.assertEqual(rodou[0][0], "sh")
        self.assertTrue(regs[0]["ok"])
        self.assertEqual(raiz.resultado(pid, self.d)["saida"], "Total reclaimed space: 2GB")
        self.assertFalse(any((self.d / "vps" / "pedidos").iterdir()))
        self.assertIn("limpar-docker", (self.d / "vps" / "consertos.jsonl").read_text(encoding="utf-8"))

    def test_protegido_e_alvo_inexistente_sao_recusados(self):
        with mock.patch.object(raiz, "_servicos", return_value={"cliente-a-site-uv2si7", "fsite06-banco-seffzk"}):
            self.assertEqual(raiz.comando_da_acao("reiniciar-servico", "cliente-a-site-uv2si7")[:3],
                             ["docker", "service", "update"])
            for alvo in ("fsite06-banco-seffzk", "dokploy-postgres", "nao-existe"):
                with self.assertRaises(ValueError):
                    raiz.comando_da_acao("reiniciar-servico", alvo)

    # test_rotinas_do_cron, test_rodar_rotina_agora_forca_e_rerodar_nao moram em tests/test_raiz_cron.py
    #: leem deploy/*.cron, o cron da nossa VPS; fora do snapshot público.

    def test_runner_ocupado_recusa(self):
        with self.assertRaises(ValueError):
            raiz.comando_da_acao("atualizar-runner", None, runner_ocupado=lambda: "00007-planejar.json.rodando")


class AprovadoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        self.cmds = "docker image prune -f"
        self.h = raiz.hash_comandos(self.cmds)
        self.pedidos = self.d / "pedidos.json"
        self.salvar_pedido("executando", self.h)
        self.fila = self.d / "root-fila"
        self.fila.mkdir()
        self.feitos = self.d / "feitos"

    def salvar_pedido(self, estado, h, dados=None):
        plano = {"tipo": "proposta", "hash": h, **({"dados": dados} if dados is not None else {})}
        self.pedidos.write_text(json.dumps({"seq": 7, "itens": {"7": {"estado": estado, "plano": plano}}}))

    def job(self, **extra):
        arq = self.fila / "7-x.json"
        arq.write_text(json.dumps({"n": 7, "comandos": self.cmds, "hash": self.h, "timeout_min": 5,
                                   "proposta": "20261008-120000-vps-saude-abcd", **extra}))
        return arq

    def rodar(self, arq, saida=(0, "a\nb\nc\nTotal reclaimed space: 0B")):
        marcadas, cmds = [], []
        reg = raiz.executar_aprovado(arq, self.d, rodar=lambda cmd, t, env=None, cwd=None: (cmds.append((cmd, t)), saida)[1],
                                     feitos=self.feitos, pedidos_bot=self.pedidos, exigir_root=False,
                                     marcar=lambda *a: marcadas.append(a), log_dir=self.d / "log")
        return reg, marcadas, cmds

    def feito(self):
        return json.loads((self.feitos / "00007-executar.json").read_text(encoding="utf-8"))

    def test_roda_os_comandos_que_o_fulano_viu(self):
        reg, marcadas, cmds = self.rodar(self.job())
        self.assertTrue(reg["ok"])
        self.assertEqual(cmds[0], (["bash", "-e", "-o", "pipefail", "-c", self.cmds], 300))
        f = self.feito()
        self.assertTrue(f["ok"])
        self.assertTrue(f["resumo"].startswith("código 0"))
        self.assertEqual(f["resumo"].splitlines()[1:], ["b", "c", "Total reclaimed space: 0B"])
        self.assertEqual(marcadas[0][1], "feita")

    def test_comandos_veem_as_credenciais_do_nimbus(self):
        envs = []
        raiz.executar_aprovado(self.job(), self.d, rodar=lambda cmd, t, env=None, cwd=None: (envs.append(env), (0, ""))[1],
                               feitos=self.feitos, pedidos_bot=self.pedidos, exigir_root=False,
                               marcar=lambda *a: None, log_dir=self.d / "log")
        self.assertIn("NIMBUS_HOME", envs[0])

    def test_falha_vira_x(self):
        reg, marcadas, _ = self.rodar(self.job(), (1, "erro feio"))
        self.assertFalse(self.feito()["ok"])
        self.assertTrue(self.feito()["erro"].startswith("código 1"))
        self.assertEqual(marcadas[0][1], "falhou")

    def test_comando_trocado_depois_da_aprovacao_e_recusado(self):
        arq = self.job(comandos="rm -rf /")
        reg, _, cmds = self.rodar(arq)
        self.assertEqual(cmds, [])
        self.assertIn("hash", reg["saida"])
        self.assertFalse(self.feito()["ok"])

    def test_hash_que_o_bot_nao_guardou_e_recusado(self):
        outro = "rm -rf /tmp/x"
        self.h = raiz.hash_comandos(outro)
        self.cmds = outro
        reg, _, cmds = self.rodar(self.job())
        self.assertEqual(cmds, [])
        self.assertIn("não foi o que o Ana aprovou", reg["saida"])

    def test_sem_host_roda_local(self):
        _, _, cmds = self.rodar(self.job())
        self.assertEqual(cmds[0][0][0], "bash")

    def test_host_servidor_a_roda_local(self):  # a servidor_a é o dedicado, onde o root roda (o ssh dava "Could not resolve")
        for host in ("vps-servidor_a", "servidor_a", "vps-nova", "dedicado"):
            self.salvar_pedido("executando", self.h, {"host": host})
            _, _, cmds = self.rodar(self.job())
            self.assertEqual(cmds[0][0][0], "bash", host)

    def test_host_servidor_b_roda_por_ssh(self):
        self.salvar_pedido("executando", self.h, {"host": "vps-servidor_b"})
        _, _, cmds = self.rodar(self.job())
        self.assertEqual(cmds[0], (["ssh", "-o", "BatchMode=yes", "vps-servidor_b",
                                    f"timeout -k 5 285 bash -e -o pipefail -c '{self.cmds}'"], 300))

    def test_host_alias_e_aspas_no_ssh(self):
        self.cmds = "echo 'a b'; hostname"
        self.h = raiz.hash_comandos(self.cmds)
        self.salvar_pedido("executando", self.h, {"host": "servidor_b"})
        _, _, cmds = self.rodar(self.job())
        self.assertEqual(cmds[0][0][:4], ["ssh", "-o", "BatchMode=yes", "vps-servidor_b"])
        self.assertEqual(shlex.split(cmds[0][0][4]), ["timeout", "-k", "5", "285", "bash", "-e", "-o", "pipefail", "-c", self.cmds])

    def test_host_servidor_hub_explicito_roda_local(self):
        self.salvar_pedido("executando", self.h, {"host": "servidor_hub"})
        _, _, cmds = self.rodar(self.job())
        self.assertEqual(cmds[0][0][0], "bash")

    def test_host_invalido_e_recusado(self):
        for ruim in ("evil-com.example", "root@vps-servidor_a", "-oProxyCommand=x", 5):
            self.salvar_pedido("executando", self.h, {"host": ruim})
            reg, _, cmds = self.rodar(self.job())
            self.assertEqual(cmds, [], ruim)
            self.assertIn("não permitido", reg["saida"])

    def test_pedido_descartado_nao_roda(self):
        self.salvar_pedido("descartado", self.h)
        _, _, cmds = self.rodar(self.job())
        self.assertEqual(cmds, [])


if __name__ == "__main__":
    unittest.main()


class FilaProcessarTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        self.fila = self.d / "root-fila"
        self.fila.mkdir()
        p = mock.patch.object(raiz, "_sha_main", return_value=None, create=True)  # sem rede (git ls-remote) na suíte
        p.start()
        self.addCleanup(p.stop)

    def test_atualizar_hub_repetidos_rodam_uma_vez_e_todos_tem_resultado(self):
        ids = [raiz.pedir("atualizar-hub", motivo=f"m{i}", base_dados=self.d) for i in range(3)]
        outro = raiz.pedir("limpar-journal", base_dados=self.d)
        rodou = []
        with mock.patch.object(raiz, "_rodar", lambda cmd, t, *a, **k: (rodou.append(cmd), (0, "ok"))[1]):
            feitos = raiz._processar(self.d, self.fila)
        self.assertEqual(len(rodou), 2)  # um atualizar-hub + o limpar-journal
        for pid in ids + [outro]:
            r = raiz.resultado(pid, self.d)
            self.assertTrue(r["ok"], pid)
        regs = [raiz.resultado(pid, self.d) for pid in ids]  # mesmo segundo: a ordem vem do sufixo aleatório
        quem = [r["id"] for r in regs if "coberto_por" not in r]
        self.assertEqual(len(quem), 1)
        self.assertEqual({r["coberto_por"] for r in regs if "coberto_por" in r}, set(quem))
        self.assertEqual(len(feitos), 4)
        self.assertEqual(list((self.d / "vps" / "pedidos").iterdir()), [])

    def test_atualizacao_ja_em_andamento_nao_cobre_os_repetidos(self):
        ids = [raiz.pedir("atualizar-hub", motivo=f"m{i}", base_dados=self.d) for i in range(3)]
        chamadas = []

        def rodar(cmd, t, *a, **k):  # a 1ª encontra o trava de outra atualização (atualizar.sh sai com 75)
            chamadas.append(cmd)
            return (raiz.ADIADO, "atualização já em andamento") if len(chamadas) == 1 else (0, "atualizado")

        with mock.patch.object(raiz, "_rodar", rodar):
            raiz._processar(self.d, self.fila)
        primeiro, segundo, terceiro = sorted(ids)  # a fila é por nome de arquivo, que é o id
        self.assertEqual(len(chamadas), 3)  # 1ª adiada; 2ª roda e cobre a 3ª; a 1ª volta em adiados/ e roda de novo
        self.assertEqual(raiz.resultado(terceiro, self.d)["coberto_por"], segundo)
        self.assertNotIn("coberto_por", raiz.resultado(primeiro, self.d))  # o adiamento não virou cobertura
        self.assertTrue(raiz.resultado(primeiro, self.d)["ok"])  # o resultado da execução que rodou, não o do adiamento
        self.assertTrue(raiz.resultado(segundo, self.d)["ok"])

    def test_adiamento_persistente_nao_gira_o_laco(self):
        ids = [raiz.pedir("atualizar-hub", motivo=f"m{i}", base_dados=self.d) for i in range(2)]
        chamadas = []

        def rodar(cmd, t, *a, **k):  # o trava nunca solta: cada um tenta uma vez na fila e uma vez em adiados/
            chamadas.append(cmd)
            return raiz.ADIADO, "atualização já em andamento"

        with mock.patch.object(raiz, "_rodar", rodar):
            raiz._processar(self.d, self.fila)
        self.assertEqual(len(chamadas), 4)  # 2 na pasta de pedidos + 2 em adiados, sem repetir na mesma execução
        self.assertEqual(sorted(p.name for p in (self.d / "vps" / "adiados").iterdir()), sorted(f"{p}.json" for p in ids))
        for pid in ids:
            self.assertNotIn("coberto_por", raiz.resultado(pid, self.d) or {})

    def test_acao_que_nao_colapsa_roda_cada_uma(self):
        for _ in range(2):
            raiz.pedir("limpar-journal", base_dados=self.d)
        rodou = []
        with mock.patch.object(raiz, "_rodar", lambda cmd, t, *a, **k: (rodou.append(cmd), (0, "ok"))[1]):
            raiz._processar(self.d, self.fila)
        self.assertEqual(len(rodou), 2)

    def test_aprovada_roda_antes_dos_pedidos_seguintes(self):
        for _ in range(3):
            raiz.pedir("limpar-journal", base_dados=self.d)
        ordem = []

        def pedido(arq, base_dados=None, rodar=None):
            ordem.append("pedido")
            arq.unlink()
            return {"ok": True}

        def aprovado(arq, base_dados=None, **k):
            ordem.append("aprovada")
            arq.unlink()
            return {"ok": True}

        def cai_na_fila_depois_do_1o(arq, base_dados=None, rodar=None):
            r = pedido(arq)
            if ordem.count("pedido") == 1:
                (self.fila / "9-x.json").write_text("{}")
            return r

        with mock.patch.object(raiz, "executar_pedido", cai_na_fila_depois_do_1o), \
                mock.patch.object(raiz, "executar_aprovado", aprovado):
            raiz._processar(self.d, self.fila)
        self.assertEqual(ordem[:2], ["pedido", "aprovada"])  # não espera os outros 2 pedidos


class AppsDoAlvoTest(unittest.TestCase):
    APPS = [
        {"name": "site", "appName": "psite15-site-eqo2je", "applicationId": "a1"},
        {"name": "site", "appName": "outro-site-abc123", "applicationId": "a2"},
    ]

    def test_appname_exato_ganha_do_regex(self):
        self.assertEqual([a["applicationId"] for a in raiz._apps_do_alvo(self.APPS, "psite15-site-eqo2je")], ["a1"])

    def test_servico_swarm_sem_exato_continua_valendo(self):
        apps = [{"name": "web", "appName": "x-web-zzzzzz", "applicationId": "b1"}]
        self.assertEqual(len(raiz._apps_do_alvo(apps, "proj-web-abc123")), 1)

    def test_nome_chutado_do_site_vira_application_id(self):
        for nome in ("fsite06", "cliente-a", "cliente-a-site", "cliente-a-v2"):
            self.assertEqual(raiz._alvo_do_site(nome), "dokploy-app-fsite06")
        self.assertEqual(raiz._alvo_do_site("psite15-site-eqo2je"), "psite15-site-eqo2je")

    def test_site_fora_do_dokploy_explica_em_vez_de_nao_achei(self):
        with mock.patch.object(raiz, "ler_env", return_value={"DOKPLOY_API_KEY": "k"}), \
                mock.patch.object(raiz, "apps_dokploy", return_value=[]):
            codigo, saida = raiz._deploy_app("clienteh")
        self.assertEqual(codigo, 1)
        self.assertIn("não está neste Dokploy", saida)

    def test_mesmo_app_repetido_conta_uma_vez(self):
        self.assertEqual(len(raiz._apps_do_alvo(self.APPS + self.APPS[:1], "psite15-site-eqo2je")), 1)


class TravaOcupadaTest(unittest.TestCase):
    """trava segura pelo saude/atualizar não pode gerar laço do nimbus-raiz.path (start-limit-hit)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        self.trava = str(self.d / "raiz.lock")

    def test_trava_ocupada_espera_antes_de_sair_sem_processar(self):
        import fcntl
        import os
        fd = os.open(self.trava, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        dormiu = []
        relogio = [0.0]

        def dorme(s):
            dormiu.append(s)
            relogio[0] += s
        with mock.patch.object(raiz, "_processar") as proc, \
                mock.patch.object(raiz.time, "sleep", dorme), \
                mock.patch.object(raiz.time, "monotonic", lambda: relogio[0]):
            r = raiz.processar(str(self.d), trava=self.trava, espera_s=3)
        self.assertEqual(r, [])
        proc.assert_not_called()
        self.assertEqual(len(dormiu), 3)  # esperou (1 s por tentativa), não saiu na hora

    def test_pega_a_trava_se_ela_solta_durante_a_espera(self):
        import fcntl
        import os
        fd = os.open(self.trava, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)

        def solta(_s):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with mock.patch.object(raiz, "_processar", return_value=["ok"]) as proc, \
                mock.patch.object(raiz.time, "sleep", solta):
            r = raiz.processar(str(self.d), trava=self.trava, espera_s=5)
        os.close(fd)
        self.assertEqual(r, ["ok"])
        proc.assert_called_once()

    def test_path_em_failed_e_religado(self):
        cmds = []

        def rodar(cmd):
            cmds.append(cmd[1])
            return mock.Mock(stdout="failed\n")
        self.assertTrue(raiz.religar_path(rodar))
        self.assertEqual(cmds, ["is-active", "reset-failed", "start"])

    def test_path_active_nao_e_tocado(self):
        cmds = []

        def rodar(cmd):
            cmds.append(cmd[1])
            return mock.Mock(stdout="active\n")
        self.assertFalse(raiz.religar_path(rodar))
        self.assertEqual(cmds, ["is-active"])

    def test_unidades_sem_start_limit(self):
        # sem isto o path cai em start-limit-hit quando a trava está ocupada e nada o religa
        base = Path(__file__).resolve().parent.parent / "deploy"
        if not (base / "nimbus-raiz.path").exists():
            self.skipTest("as unidades do root não vão no snapshot público")
        for u in ("nimbus-raiz.path", "nimbus-raiz.service"):
            self.assertIn("StartLimitIntervalSec=0", (base / u).read_text(encoding="utf-8"), u)


SHA_A = "a" * 40
SHA_B = "b" * 40
SAIDA_REPROVADA = ("ATUALIZAR_SHA=%s\nATUALIZAR_TESTES_REPROVADOS\n"
                   "FAIL: test_data_nao_aponta_para_a_vps (tests.test_guarda_dados.GuardaDados)\n"
                   "ERROR: test_segredos_so_nomes (tests.test_grafo.Grafo)\nFAILED (failures=1, errors=1)")


class MesmaHeadReprovadaTest(unittest.TestCase):
    """a main que reprovou nos testes não roda a suíte de novo; HEAD nova roda."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        self.chamadas = []
        self.assertTrue(hasattr(raiz, "estado_atualizar"), "falta o estado do atualizar-hub (KRN-1808)")

    def pedido(self, sha_main, saida="ATUALIZAR_SHA=%s\nok", codigo=0, motivo="merge"):
        pid = raiz.pedir("atualizar-hub", motivo=motivo, base_dados=self.d)
        arq = self.d / "vps" / "pedidos" / f"{pid}.json"

        def rodar(cmd, t, *a, **k):
            self.chamadas.append(cmd)
            return codigo, saida.replace("%s", str(sha_main)) if "%s" in saida else saida

        return raiz.executar_pedido(arq, self.d, rodar=rodar, sha_main=lambda: sha_main)

    def test_mesma_head_reprovada_e_recusada_sem_rodar(self):
        r = self.pedido(SHA_A, SAIDA_REPROVADA % SHA_A, codigo=1)
        self.assertEqual((r["codigo"], r["sha"]), (1, SHA_A))
        self.assertEqual(len(self.chamadas), 1)
        r2 = self.pedido(SHA_A)
        self.assertEqual(r2["codigo"], 2)
        self.assertIn("mesma HEAD já reprovada: conserte os testes antes", r2["saida"])
        self.assertEqual(r2["sha"], SHA_A)
        self.assertEqual(len(self.chamadas), 1)  # não rodou a suíte

    def test_recusa_com_muitos_testes_mantem_a_frase(self):  # o resultado guarda só os últimos 1500 caracteres
        falhas = "".join(f"FAIL: test_{n} (tests.test_x.T{n})\n" for n in range(20))
        self.pedido(SHA_A, "ATUALIZAR_SHA=%s\nATUALIZAR_TESTES_REPROVADOS\n" + falhas + "FAILED (failures=20)", codigo=1)
        r2 = self.pedido(SHA_A)
        self.assertIn("mesma HEAD já reprovada: conserte os testes antes", r2["saida"])
        self.assertIn("(+15)", r2["saida"])
        self.assertLessEqual(len(r2["saida"]), 1500)

    def test_head_nova_roda(self):
        self.pedido(SHA_A, SAIDA_REPROVADA % SHA_A, codigo=1)
        r = self.pedido(SHA_B)
        self.assertEqual((r["codigo"], r["sha"]), (0, SHA_B))
        self.assertEqual(len(self.chamadas), 2)
        self.assertNotIn("reprovada", raiz.estado_atualizar(self.d))

    def test_forcar_no_motivo_roda_de_novo(self):
        self.pedido(SHA_A, SAIDA_REPROVADA % SHA_A, codigo=1)
        r = self.pedido(SHA_A, motivo="teste instável [forcar]")
        self.assertEqual(r["codigo"], 0)
        self.assertEqual(len(self.chamadas), 2)

    def test_falha_que_nao_e_de_teste_nao_trava_a_head(self):
        self.pedido(SHA_A, "ATUALIZAR_SHA=%s\nERRO: pull" % SHA_A, codigo=1)  # sem o marcador de testes reprovados
        self.assertEqual(self.pedido(SHA_A)["codigo"], 0)

    def test_sem_sha_da_main_nao_recusa(self):
        self.pedido(SHA_A, SAIDA_REPROVADA % SHA_A, codigo=1)
        self.assertEqual(self.pedido(None)["codigo"], 0)

    def test_recusa_e_rapida(self):
        import time
        self.pedido(SHA_A, SAIDA_REPROVADA % SHA_A, codigo=1)
        t = time.monotonic()
        self.pedido(SHA_A)
        self.assertLess(time.monotonic() - t, 1)

    def test_testes_reprovados_ficam_no_estado(self):
        self.pedido(SHA_A, SAIDA_REPROVADA % SHA_A, codigo=1)
        self.assertEqual(raiz.estado_atualizar(self.d)["reprovada"]["testes"],
                         ["tests.test_guarda_dados.GuardaDados.test_data_nao_aponta_para_a_vps",
                          "tests.test_grafo.Grafo.test_segredos_so_nomes"])

    def test_modulo_que_nao_importa_entra_na_lista(self):
        saida = "ATUALIZAR_SHA=%s\nATUALIZAR_TESTES_REPROVADOS\nERROR: tests.test_x (unittest.loader._FailedTest.tests.test_x)" % SHA_A
        self.pedido(SHA_A, saida, codigo=1)
        self.assertEqual(raiz.estado_atualizar(self.d)["reprovada"]["testes"], ["tests.test_x"])


class AtualizarCronTest(MesmaHeadReprovadaTest):
    """o cron de 5 min passa pela mesma trava e grava o mesmo estado."""

    def cron(self, sha_main, saida, codigo):
        def rodar(cmd, t, *a, **k):
            self.chamadas.append(cmd)
            return codigo, saida

        return raiz.atualizar_cron(self.d, rodar=rodar, sha_main=lambda: sha_main)

    def test_cron_nao_repete_a_head_reprovada(self):
        self.assertEqual(self.cron(SHA_A, SAIDA_REPROVADA % SHA_A, 1)[0], 1)
        self.assertEqual(raiz.estado_atualizar(self.d)["reprovada"]["sha"], SHA_A)
        codigo, texto = self.cron(SHA_A, "ok", 0)
        self.assertEqual(codigo, 0)
        self.assertIn("mesma HEAD já reprovada", texto)
        self.assertEqual(len(self.chamadas), 1)
        self.assertEqual(self.cron(SHA_B, "ATUALIZAR_SHA=%s\nok" % SHA_B, 0)[0], 0)
        self.assertEqual(len(self.chamadas), 2)

    def test_cron_com_trava_ocupada_nao_cria_adiado(self):
        codigo, _ = self.cron(SHA_A, "atualização já em andamento", raiz.ADIADO)
        self.assertEqual(codigo, 0)
        self.assertFalse((self.d / "vps" / "adiados").exists())


class AtrasoDoHubTest(unittest.TestCase):
    """hub mais de 2 h atrás da main abre um incidente hub_quebrado_em_producao só."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        self.t0 = raiz.dt.datetime(2026, 10, 22, 22, 0, tzinfo=raiz.UTC)
        self.assertTrue(hasattr(raiz, "_vigiar_atraso"), "falta o vigia do atraso do hub (KRN-1808)")

    def falhar(self, quando):
        raiz._guardar_resultado_atualizar(self.d, 1, SAIDA_REPROVADA % SHA_A, SHA_A, agora=quando)
        return raiz._vigiar_atraso(self.d, agora=quando)

    def test_menos_de_2h_nao_abre(self):
        self.assertIsNone(self.falhar(self.t0))
        self.assertIsNone(self.falhar(self.t0 + raiz.dt.timedelta(minutes=119)))

    def test_depois_de_2h_abre_um_so_com_os_testes(self):
        from hub import incidentes
        self.falhar(self.t0)
        inc = self.falhar(self.t0 + raiz.dt.timedelta(hours=2, minutes=1))
        self.assertEqual(inc["classe"], "hub_quebrado_em_producao")
        self.assertIn("test_data_nao_aponta_para_a_vps", inc["falha"])
        self.assertIsNone(self.falhar(self.t0 + raiz.dt.timedelta(hours=3)))  # já aberto: não abre outro
        self.assertEqual(len(incidentes.listar(so_pendentes=True, base_dados=self.d, agora=self.t0 + raiz.dt.timedelta(hours=3))), 1)

    def test_sucesso_zera_o_atraso_e_fecha_o_incidente(self):
        from hub import incidentes
        self.falhar(self.t0)
        agora = self.t0 + raiz.dt.timedelta(hours=3)
        self.falhar(agora)
        raiz._guardar_resultado_atualizar(self.d, 0, "ok", SHA_B, agora=agora)
        self.assertEqual(incidentes.listar(so_pendentes=True, base_dados=self.d, agora=agora), [])
        self.assertEqual(raiz.estado_atualizar(self.d)["instalado_sha"], SHA_B)
        self.assertIsNone(raiz._vigiar_atraso(self.d, agora=agora + raiz.dt.timedelta(hours=5)))
