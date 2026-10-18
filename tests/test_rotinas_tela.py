import datetime as dt
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from hub import agenda, raiz, config, config_telas, conta, painel, rotinas, rotinas_tela, tarefas as T, ui

TXT = ("---\nquando: 0 8 * * *\nprioridade: 2\nmodelo: haiku\ntimeout: 10m\nrepos: org/a\nexterno: false\n"
       "descricao: Faz a coisa\n---\nCorpo da tarefa.\n")
CRON = ("15 6 * * * root /opt/x/deploy/rotina.sh geo /bin/bash deploy/geo.sh >> {log} 2>&1\n"
        "0 7 * * * root /opt/x/deploy/rotina.sh coleta /bin/bash deploy/collect.sh >> /nao/existe.log 2>&1\n")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.dados, self.tar = self.dir / "dados", self.dir / "tarefas"
        self.dados.mkdir()
        self.tar.mkdir()
        (self.tar / "uma-tarefa.md").write_text(TXT, encoding="utf-8")
        (self.tar / "manual-x.md").write_text(TXT.replace("0 8 * * *", "manual"), encoding="utf-8")
        self.log = self.dir / "geo.log"
        self.log.write_text("2026-10-16T06:15:00Z geo: início (x)\n<b>x</b>\n2026-10-16T06:15:30Z geo: fim, código 0\n", encoding="utf-8")
        cron = self.dir / "x.cron"
        cron.write_text(CRON.format(log=self.log), encoding="utf-8")
        toml = self.dir / "k.toml"
        toml.write_text(f'[rotinas]\ncron = "{cron}"\n[rotinas.horarios]\nplug = "0 9 * * *"\n', encoding="utf-8")
        env = {"KRONOS_CONFIG": str(toml), "KRONOS_CAMINHOS_DADOS": str(self.dados), "KRONOS_TAREFAS": str(self.tar)}
        for p in (mock.patch.dict(os.environ, env), mock.patch.object(rotinas.plugins, "rotinas", return_value={"plug": "deploy/rotina.sh plug x"})):
            p.start()
            self.addCleanup(p.stop)
        config.recarregar()
        self.addCleanup(config.recarregar)
        self.csrf = painel._csrf()

    def post(self, campos, csrf_ok=True, usuario="ana"):
        return config_telas.tratar_post("/config/rotinas", {"csrf": self.csrf, **campos}, usuario, "1.2.3.4", csrf_ok)

    def pagina(self, q=None):
        return config_telas.ROTAS_GET["/config/rotinas"](q or {}, "ana", self.csrf)


class ListaTest(Base):
    def test_uma_linha_por_tarefa_e_rotina(self):
        st, h = self.pagina()
        self.assertEqual(st, 200)
        for nome in ("uma-tarefa", "manual-x", "geo", "coleta", "plug"):
            self.assertIn(f'<b class="mono">{nome}</b>', h)
        self.assertIn("tarefa do Claude", h)
        self.assertIn("rotina do sistema", h)
        self.assertIn("rotina de plugin", h)
        self.assertIn("próxima:", h)
        self.assertIn("Brasília", h)
        self.assertIn("UTC", h)
        self.assertIn('href="/metas"', h)

    def test_menu_tem_rotinas(self):
        self.assertIn("/config/rotinas", [c for g in ui.MENU if g[0] == "Configurar" for c, _ in g[2]])

    def test_ultimo_resultado_de_rotina_e_link_do_log(self):
        _, h = self.pagina()
        self.assertIn("30 s", h)
        self.assertIn("/config/rotinas/log?tipo=rotina&amp;nome=geo", h)
        self.assertIn("sem registro", h)

    def test_ultimo_resultado_de_tarefa(self):
        agora = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.timezone.utc)
        saida = agenda.HOME / "saida"
        reg = {"inicio": "2026-10-17T11:00:00+00:00", "fim": "2026-10-17T11:02:00+00:00", "nome": "uma-tarefa", "ok": False,
               "duracao_s": 120.0, "saida": str(saida / "uma-tarefa" / "x.md")}
        conta.registrar(reg, str(self.dados))
        h = rotinas_tela.rotinas_html(self.csrf, "ana", agora, str(self.dados))
        self.assertIn("erro", h)
        self.assertIn("2 min", h)
        self.assertIn("/config/rotinas/log?tipo=tarefa&amp;nome=uma-tarefa", h)

    def test_tarefa_manual_nao_tem_liga_desliga(self):
        _, h = self.pagina()
        self.assertIn("só a pedido", h)

    def test_html_escapado(self):
        (self.tar / "xss.md").write_text(TXT.replace("Faz a coisa", "<script>alert(1)</script>"), encoding="utf-8")
        (self.tar / "quebrada.md").write_text("---\nquando: <img src=x onerror=1>\n---\n", encoding="utf-8")
        _, h = self.pagina()
        self.assertNotIn("<script>alert", h)
        self.assertNotIn("<img src=x", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertIn("com erro", h)


class LigarTest(Base):
    def test_desligar_grava_na_sobreposicao_e_agenda_respeita(self):
        st, dest = self.post({"acao": "desligar", "nome": "uma-tarefa", "tipo": "tarefa"})
        self.assertEqual(st, 303)
        self.assertIn("ok=", dest)
        self.assertEqual(config.get("rotinas.desligadas"), ["tarefa:uma-tarefa"])
        self.assertTrue((self.dados / "kronos.painel.toml").is_file())
        t = T.validar(TXT, "uma-tarefa")
        antes = dt.datetime(2026, 10, 17, 7, 58, tzinfo=T.BRT)
        agenda.agendar({"uma-tarefa": t}, antes, str(self.dados))
        self.assertEqual(agenda.agendar({"uma-tarefa": t}, antes + dt.timedelta(minutes=5), str(self.dados)), [])
        _, h = self.pagina()
        self.assertIn("desligada: não dispara sozinha", h)
        self.assertIn("Ligar", h)

    def test_religar(self):
        self.post({"acao": "desligar", "nome": "geo", "tipo": "rotina"})
        self.assertEqual(config.get("rotinas.desligadas"), ["geo"])
        self.assertEqual(rotinas.devidas(dt.datetime(2026, 10, 18, 6, 15)), [])
        self.post({"acao": "ligar", "nome": "geo", "tipo": "rotina"})
        self.assertEqual(config.get("rotinas.desligadas"), [])
        self.assertEqual(len(rotinas.devidas(dt.datetime(2026, 10, 18, 6, 15))), 1)

    def test_rotina_e_tarefa_de_mesmo_nome_nao_se_confundem(self):
        (self.tar / "geo.md").write_text(TXT, encoding="utf-8")
        self.post({"acao": "desligar", "nome": "geo", "tipo": "tarefa"})
        self.assertEqual(config.get("rotinas.desligadas"), ["tarefa:geo"])
        self.assertEqual(len(rotinas.devidas(dt.datetime(2026, 10, 18, 6, 15))), 1)

    def test_nome_desconhecido_ou_malicioso(self):
        for nome in ("../x", "nao-existe", "geo\nx", ""):
            st, dest = self.post({"acao": "desligar", "nome": nome, "tipo": "rotina"})
            self.assertIn("erro=", dest)
        self.assertEqual(config.get("rotinas.desligadas"), [])

    def test_sem_csrf_403_e_nada_muda(self):
        self.assertEqual(self.post({"acao": "desligar", "nome": "geo", "tipo": "rotina"}, csrf_ok=False), (403, None))
        self.assertEqual(config.get("rotinas.desligadas"), [])
        self.assertEqual(self.post({"acao": "salvar", "nome": "uma-tarefa"}, csrf_ok=False), (403, None))
        self.assertEqual(self.post({"acao": "rodar", "nome": "uma-tarefa", "tipo": "tarefa"}, csrf_ok=False), (403, None))


class RodarTest(Base):
    def test_tarefa_entra_na_fila(self):
        with mock.patch.object(agenda, "enfileirar", wraps=lambda t, m, *a, **k: True) as enf:
            st, dest = self.post({"acao": "rodar", "nome": "uma-tarefa", "tipo": "tarefa"})
        self.assertEqual(st, 303)
        self.assertIn("ok=", dest)
        self.assertEqual(enf.call_args.args[0].nome, "uma-tarefa")
        self.assertEqual(enf.call_args.args[1], "manual (painel)")

    def test_tarefa_fila_real_e_repetida(self):
        with mock.patch.object(conta, "pasta", lambda base=None: self.dados / "claude"):
            self.post({"acao": "rodar", "nome": "uma-tarefa", "tipo": "tarefa"})
            self.assertEqual([i["nome"] for i in agenda.fila()], ["uma-tarefa"])
            _, dest = self.post({"acao": "rodar", "nome": "uma-tarefa", "tipo": "tarefa"})
            self.assertIn("j%C3%A1", dest)

    def test_desligada_roda_agora_mesmo_assim(self):
        self.post({"acao": "desligar", "nome": "uma-tarefa", "tipo": "tarefa"})
        with mock.patch.object(agenda, "enfileirar", return_value=True) as enf:
            self.post({"acao": "rodar", "nome": "uma-tarefa", "tipo": "tarefa"})
        enf.assert_called_once()

    def test_rotina_de_plugin_dispara_capturado_como_usuario_do_painel(self):
        with mock.patch.object(rotinas, "disparar_capturado") as d, mock.patch.object(raiz, "pedir") as pr:
            st, dest = self.post({"acao": "rodar", "nome": "plug", "tipo": "rotina"})
        self.assertIn("ok=", dest)
        self.assertEqual(d.call_args.args[0]["comando"], "deploy/rotina.sh plug x")
        pr.assert_not_called()

    def test_comando_vem_do_catalogo_nao_do_formulario(self):
        with mock.patch.object(rotinas, "disparar_capturado") as d:
            self.post({"acao": "rodar", "nome": "plug", "tipo": "rotina", "comando": "rm -rf /"})
        self.assertNotIn("rm", d.call_args.args[0]["comando"])

    def test_processo_que_nao_sobe_nao_diz_iniciada(self):
        with mock.patch.object(rotinas.subprocess, "Popen", side_effect=OSError("sem bash")):
            _, dest = self.post({"acao": "rodar", "nome": "plug", "tipo": "rotina"})
        self.assertIn("erro=", dest)
        self.assertNotIn("rodando", urllib.parse.unquote(dest))

    def test_pasta_do_log_sem_permissao_vira_mensagem_na_tela(self):
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError), mock.patch.object(rotinas.subprocess, "Popen") as po:
            _, dest = self.post({"acao": "rodar", "nome": "plug", "tipo": "rotina"})
        po.assert_not_called()
        msg = urllib.parse.unquote(dest)
        self.assertIn("erro=", msg)
        self.assertIn("não é gravável pelo usuário do painel", msg)
        self.assertIn("sudo install -d -o", msg)

    def test_rotina_do_sistema_vai_pelo_root(self):
        with mock.patch.object(raiz, "pedir", return_value="20261017-120000-rodar-rotina-agora-ab12") as pr, \
                mock.patch.object(rotinas, "disparar_capturado") as d:
            _, dest = self.post({"acao": "rodar", "nome": "geo", "tipo": "rotina"})
        d.assert_not_called()
        self.assertEqual(pr.call_args.args[:2], ("rodar-rotina-agora", "geo"))
        self.assertIn("ok=", dest)
        self.assertIn("enviado ao root", urllib.parse.unquote(dest))
        _, h = self.pagina()
        self.assertIn("sem resultado ainda", h)

    def test_resultado_do_root_aparece_ao_recarregar(self):
        pid = "20261017-120000-rodar-rotina-agora-ab12"
        with mock.patch.object(raiz, "pedir", return_value=pid):
            self.post({"acao": "rodar", "nome": "geo", "tipo": "rotina"})
        res = self.dados / "vps" / "resultados"
        res.mkdir(parents=True)
        reg = {"id": pid, "em": "2026-10-17T12:00:00+00:00", "codigo": 1, "ok": False, "duracao_s": 3.0, "saida": "deu <b>ruim</b>"}
        (res / f"{pid}.json").write_text(json.dumps(reg), encoding="utf-8")
        _, h = self.pagina()
        i = h.index('<b class="mono">geo</b>')
        linha = h[i:h.index("</tr>", i)]
        self.assertIn("erro", linha)
        self.assertIn("fonte=raiz", linha)
        _, log = config_telas.ROTAS_GET["/config/rotinas/log"]({"tipo": "rotina", "nome": "geo", "fonte": "raiz"}, "ana", self.csrf)
        self.assertIn("deu &lt;b&gt;ruim&lt;/b&gt;", log)
        (res / f"{pid}.json").write_text(json.dumps({**reg, "codigo": 79}), encoding="utf-8")
        _, h = self.pagina()
        self.assertIn("pulada: já estava rodando", h)
        (res / f"{pid}.json").write_text(json.dumps({**reg, "codigo": 0, "ok": True}), encoding="utf-8")
        _, h = self.pagina()
        i = h.index('<b class="mono">geo</b>')
        self.assertIn('selo ok">ok', h[i:h.index("</tr>", i)])

    def test_resultado_do_root_com_saida_vazia_aponta_para_o_log_do_cron(self):
        pid = "20261017-120000-rodar-rotina-agora-ab12"
        with mock.patch.object(raiz, "pedir", return_value=pid):
            self.post({"acao": "rodar", "nome": "geo", "tipo": "rotina"})
        res = self.dados / "vps" / "resultados"
        res.mkdir(parents=True)
        reg = {"id": pid, "em": "2026-10-17T12:00:00+00:00", "codigo": 0, "ok": True, "duracao_s": 3.0, "saida": ""}
        (res / f"{pid}.json").write_text(json.dumps(reg), encoding="utf-8")
        self.log.write_text("linha do log de verdade <i>x</i>\n", encoding="utf-8")
        _, h = self.pagina()
        i = h.index('<b class="mono">geo</b>')
        self.assertIn("fonte=raiz", h[i:h.index("</tr>", i)])
        st, log = config_telas.ROTAS_GET["/config/rotinas/log"]({"tipo": "rotina", "nome": "geo", "fonte": "raiz"}, "ana", self.csrf)
        self.assertEqual(st, 200)
        self.assertIn("linha do log de verdade &lt;i&gt;x&lt;/i&gt;", log)
        self.assertNotIn("<i>x</i>", log)

    def test_pedido_com_id_lixo_nao_da_500_na_tela(self):
        arq = rotinas._arq_pedido("geo")
        arq.parent.mkdir(parents=True)
        for lixo in ('{"id": 123}', '{"id": {"a": 1}}', 'lixo'):
            arq.write_text(lixo, encoding="utf-8")
            self.assertEqual(self.pagina()[0], 200)
            st, _ = config_telas.ROTAS_GET["/config/rotinas/log"]({"tipo": "rotina", "nome": "geo", "fonte": "raiz"}, "ana", self.csrf)
            self.assertEqual(st, 404)

    def test_hub_raiz_com_erro_mostra_o_erro_real(self):
        with mock.patch.object(raiz, "pedir", side_effect=ValueError("rotina geo não está no cron")):
            _, dest = self.post({"acao": "rodar", "nome": "geo", "tipo": "rotina"})
        self.assertIn("erro=", dest)
        self.assertIn("rotina geo não está no cron", urllib.parse.unquote(dest))
        with mock.patch.object(raiz, "pedir", side_effect=PermissionError("negado")):
            _, dest = self.post({"acao": "rodar", "nome": "geo", "tipo": "rotina"})
        self.assertIn("negado", urllib.parse.unquote(dest))

    def test_resultado_real_de_comando_que_falha_aparece_na_tela(self):
        item = {"nome": "geo", "comando": "echo <b>quebrou</b>; exit 4"}
        rotinas.executar_capturado(item, str(self.dados))  # o que o processo solto faz
        _, h = self.pagina()
        i = h.index('<b class="mono">geo</b>')
        linha = h[i:h.index("</tr>", i)]
        self.assertIn("erro", linha)
        self.assertIn("fonte=manual", linha)
        st, log = config_telas.ROTAS_GET["/config/rotinas/log"]({"tipo": "rotina", "nome": "geo", "fonte": "manual"}, "ana", self.csrf)
        self.assertEqual(st, 200)
        self.assertIn("&lt;b&gt;quebrou&lt;/b&gt;", log)


class EditarTest(Base):
    def campos(self, **extra):
        c = {"acao": "salvar", "nome": "uma-tarefa", "quando": "30 9 * * 1", "prioridade": "1", "modelo": "sonnet", "timeout": "20m",
             "repos": "org/a, org/b", "externo": "true", "descricao": "Nova descrição", "pauta": "", "avisar": "false", "skills": "", "conta": "",
             "corpo": "Novo corpo.\n\nDuas linhas."}
        return c | extra

    def test_formulario_traz_os_campos_do_contrato(self):
        st, h = self.pagina({"editar": "uma-tarefa"})
        self.assertEqual(st, 200)
        for campo in ("quando", "prioridade", "modelo", "timeout", "repos", "externo", "descricao", "pauta", "avisar", "skills", "conta", "corpo"):
            self.assertIn(f'name="{campo}"', h)
        self.assertIn("Corpo da tarefa.", h)
        self.assertIn(f'name="csrf" value="{self.csrf}"', h)

    def test_grava_e_a_agenda_le_de_volta(self):
        st, dest = self.post(self.campos())
        self.assertEqual((st, dest.split("?")[0]), (303, "/config/rotinas"))
        self.assertIn("ok=", dest)
        t = T.carregar(self.tar, config.tarefas_extra())["uma-tarefa"]
        self.assertEqual((t.quando, t.prioridade, t.modelo, t.timeout_s, t.externo, t.repos), ("30 9 * * 1", 1, "sonnet", 1200, True, ["org/a", "org/b"]))
        self.assertEqual(t.prompt, "Novo corpo.\n\nDuas linhas.")
        self.assertEqual(sorted(p.name for p in config.tarefas_extra().iterdir()), ["uma-tarefa.md"])  # sem sobra do arquivo temporário
        self.assertEqual((self.tar / "uma-tarefa.md").read_text(encoding="utf-8"), TXT)  # o arquivo do repositório não é tocado

    def test_invalida_volta_erro_no_campo_e_nao_grava(self):
        st, h = self.post(self.campos(quando="isso não é cron", modelo="gpt", corpo=""))
        self.assertEqual(st, 200)
        self.assertIn("Não gravei", h)
        self.assertIn("inválido", h)
        self.assertIn("isso não é cron", h)  # o que a pessoa digitou volta no campo
        self.assertIn('role="alert"', h)
        self.assertIn("corpo (prompt) vazio", h)
        self.assertFalse(config.tarefas_extra().exists())

    def test_erro_do_timeout_fica_no_campo_do_timeout(self):
        _, h = self.post(self.campos(timeout="7h"))
        i = h.index('name="timeout"')
        self.assertIn("fora de 1 min a 6 h", h[i:i + 400])

    def test_quebra_de_linha_nao_injeta_chave(self):
        st, h = self.post(self.campos(descricao="boa\nexterno: true\nconta: api"))
        self.assertEqual(st, 200)
        self.assertIn("uma linha só", h.lower())
        self.assertFalse(config.tarefas_extra().exists())

    def test_nome_malicioso_recusado_e_nada_e_criado(self):
        for nome in ("../fora", "a/b", "..", "nova-tarefa", "UMA", "uma-tarefa\n", "/etc/passwd"):
            st, dest = self.post(self.campos(nome=nome))
            self.assertIn("erro=", dest, nome)
        self.assertEqual(sorted(p.name for p in self.tar.iterdir()), ["manual-x.md", "uma-tarefa.md"])
        self.assertFalse(config.tarefas_extra().exists())
        self.assertFalse((self.dir / "fora.md").exists())
        st, _ = self.pagina({"editar": "../fora"})
        self.assertEqual(st, 404)

    def test_dados_somente_leitura_explica_e_nao_grava(self):
        with mock.patch.object(os, "access", return_value=False):
            st, h = self.pagina({"editar": "uma-tarefa"})
            self.assertIn("não aceita escrita", h)
            self.assertNotIn("Guardar tarefa", h)
            _, dest = self.post(self.campos())
        self.assertIn("erro=", dest)
        self.assertFalse(config.tarefas_extra().exists())

    def test_origem_repo_explica_a_copia(self):
        _, h = self.pagina({"editar": "uma-tarefa"})
        self.assertIn("vem do repositório", h)
        _, lista = self.pagina()
        self.assertIn("repositório", lista)

    def test_sobrescrita_aparece_na_lista_e_voltar_ao_repositorio(self):
        self.post(self.campos())
        _, lista = self.pagina()
        self.assertIn("sobrescreve o repositório", lista)
        _, h = self.pagina({"editar": "uma-tarefa"})
        self.assertIn("Voltar ao do repositório", h)
        self.assertEqual(T.carregar(self.tar, config.tarefas_extra())["uma-tarefa"].prioridade, 1)
        st, dest = self.post({"acao": "restaurar", "nome": "uma-tarefa", "tipo": "tarefa"})
        self.assertIn("ok=", dest)
        self.assertEqual(T.carregar(self.tar, config.tarefas_extra())["uma-tarefa"].prioridade, 2)
        self.assertFalse((config.tarefas_extra() / "uma-tarefa.md").exists())
        self.assertEqual(len(list((config.tarefas_extra() / "arquivo").glob("*uma-tarefa.md"))), 1)  # arquivada, não apagada
        self.assertEqual((self.tar / "uma-tarefa.md").read_text(encoding="utf-8"), TXT)

    def test_restaurar_so_com_copia_do_painel(self):
        _, dest = self.post({"acao": "restaurar", "nome": "uma-tarefa", "tipo": "tarefa"})
        self.assertIn("erro=", dest)
        _, dest = self.post({"acao": "restaurar", "nome": "../x", "tipo": "tarefa"})
        self.assertIn("erro=", dest)

    def test_criar_tarefa_nova(self):
        st, h = self.pagina({"novo": "1"})
        self.assertEqual(st, 200)
        self.assertIn('name="nome"', h)
        st, dest = self.post(self.campos(nome="nova-tarefa", modo="criar"))
        self.assertEqual(st, 303)
        self.assertIn("ok=", dest)
        self.assertIn("nova-tarefa", T.carregar(self.tar, config.tarefas_extra()))
        _, lista = self.pagina()
        self.assertIn("painel", lista)
        self.assertIn('nova-tarefa', lista)

    def test_criar_recusa_nome_ruim_ou_existente_com_erro_no_campo(self):
        for nome in ("../x", "Maiuscula", "a b", "uma-tarefa", ""):
            st, h = self.post(self.campos(nome=nome, modo="criar"))
            self.assertEqual(st, 200, nome)
            i = h.index('name="nome"')
            self.assertIn('role="alert"', h[i:i + 600], nome)
        self.assertFalse(config.tarefas_extra().exists())

    def test_criar_invalida_nao_grava(self):
        st, h = self.post(self.campos(nome="nova-tarefa", modo="criar", quando="x"))
        self.assertEqual(st, 200)
        self.assertFalse((config.tarefas_extra() / "nova-tarefa.md").exists())

    def test_arquivo_com_nome_invalido_aparece_na_lista(self):
        (self.tar / "Nome_Ruim.md").write_text(TXT, encoding="utf-8")
        _, h = self.pagina()
        self.assertIn("Nome_Ruim", h)
        self.assertIn("nome inválido", h)

    def test_href_com_nome_e_quotado(self):
        _, h = self.pagina()
        self.assertIn("/config/rotinas?editar=uma-tarefa", h)
        self.assertEqual(rotinas_tela._q("a b/ç&x=1"), "a%20b%2F%C3%A7%26x%3D1")  # nome que não passa RE_NOME: nunca sai cru num href
        (self.tar / "Nome Ruim.md").write_text(TXT, encoding="utf-8")
        _, h = self.pagina()
        self.assertNotIn("editar=Nome Ruim", h)


    def test_texto_de_fora_escapado_no_formulario(self):
        (self.tar / "uma-tarefa.md").write_text(TXT.replace("Corpo da tarefa.", "</textarea><script>x()</script>"), encoding="utf-8")
        _, h = self.pagina({"editar": "uma-tarefa"})
        self.assertNotIn("<script>x()", h)
        self.assertIn("&lt;/textarea&gt;", h)


class LogTest(Base):
    def get(self, **q):
        return config_telas.ROTAS_GET["/config/rotinas/log"](q, "ana", self.csrf)

    def test_log_de_rotina_escapado(self):
        st, h = self.get(tipo="rotina", nome="geo")
        self.assertEqual(st, 200)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", h)
        self.assertNotIn("<b>x</b>", h)

    def test_nome_fora_do_catalogo_404(self):
        for q in ({"tipo": "rotina", "nome": "../../etc/passwd"}, {"tipo": "tarefa", "nome": "../x"}, {"tipo": "rotina", "nome": "plug"}, {}):
            self.assertEqual(self.get(**q)[0], 404)

    def test_saida_de_tarefa_so_dentro_da_pasta_de_saida(self):
        fora = self.dir / "segredo.txt"
        fora.write_text("conteudo", encoding="utf-8")
        agora = conta.agora()
        conta.registrar({"inicio": conta.iso(agora), "fim": conta.iso(agora), "nome": "uma-tarefa", "ok": True, "saida": str(fora)}, str(self.dados))
        with mock.patch.object(conta, "pasta", lambda base=None: self.dados / "claude"):
            self.assertEqual(self.get(tipo="tarefa", nome="uma-tarefa")[0], 404)


class PainelTest(Base):
    def test_rota_do_painel_serve_a_tela(self):
        with mock.patch.object(painel, "_csrf", lambda: self.csrf):
            st, corpo, *_ = painel._rota("/config/rotinas")
        self.assertEqual(st, 200)
        self.assertIn("Rotinas e Metas", corpo)


if __name__ == "__main__":
    unittest.main()
