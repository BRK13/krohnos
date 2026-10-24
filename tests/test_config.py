import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hub
from hub import config

# a org do legado da VPS vem de privado/legado-vps.json (fora do snapshot): o teste lê o valor do próprio legado
ORG = config.LEGADO_VALOR.get("github.org", "")

CHAVES = ("CLAUDE_HOME", "HUB_DATA", "NIMBUS_HOME", "KROHNOS_CAMINHOS_DADOS", "KROHNOS_CONFIG")


class ConfigBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if k not in CHAVES}
        env["KROHNOS_CONFIG"] = str(self.dir / "nao-existe.toml")
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(config.recarregar)
        config.recarregar()

    def toml(self, texto):
        arq = self.dir / "krohnos.toml"
        arq.write_text(texto)
        os.environ["KROHNOS_CONFIG"] = str(arq)
        config.recarregar()
        return arq


class TestConfig(ConfigBase):
    def test_padrao_neutro(self):
        self.assertEqual(config.get("caminhos.dados"), "/var/lib/krohnos")
        self.assertEqual(config.get("produto.nome"), "Krohnos")

    def test_apelido_legado_vence_toml(self):
        self.toml('[caminhos]\ndados = "/do/toml"\n')
        with mock.patch.dict(os.environ, {"HUB_DATA": "/legado"}):  # sem restaurar, o HUB_DATA vazava para a suíte inteira
            self.assertEqual(config.get("caminhos.dados"), "/legado")

    def test_env_krohnos_vence_apelido_e_toml(self):
        self.toml('[caminhos]\ndados = "/do/toml"\n')
        with mock.patch.dict(os.environ, {"HUB_DATA": "/legado", "KROHNOS_CAMINHOS_DADOS": "/env"}):
            self.assertEqual(config.get("caminhos.dados"), "/env")

    def test_toml_vence_padrao_e_converte_tipos(self):
        self.toml('[painel]\nporta = 9000\n[agentes]\ncontas = ["1", "2"]\n')
        self.assertEqual(config.get("painel.porta"), 9000)
        self.assertEqual(config.get("agentes.contas"), ["1", "2"])
        os.environ["KROHNOS_PAINEL_PORTA"] = "9100"
        self.assertEqual(config.get("painel.porta"), 9100)

    def test_painel_proxies(self):
        self.assertEqual(config.get("painel.proxies"), [])  # vazio = loopback e rede privada (hub/painel.py)
        self.toml('[painel]\nproxies = ["172.19.0.0/16"]\n')
        self.assertEqual(config.get("painel.proxies"), ["172.19.0.0/16"])

    def test_chave_desconhecida_no_toml(self):
        self.toml('[caminhos]\nxpto = "a"\n')
        with self.assertRaisesRegex(config.ConfigError, "caminhos.xpto"):
            config.validar()

    def test_secao_desconhecida(self):
        self.toml('[xyz]\na = 1\n')
        with self.assertRaisesRegex(config.ConfigError, "xyz"):
            config.validar()

    def test_get_chave_desconhecida(self):
        with self.assertRaisesRegex(config.ConfigError, "caminhos.xpto"):
            config.get("caminhos.xpto")

    def test_segredo_no_toml_e_recusado(self):
        for linha in ('senha = "x"', 'token = "x"'):
            with self.subTest(linha):
                self.toml(f'[painel]\n{linha}\n')
                with self.assertRaisesRegex(config.ConfigError, "cofre"):
                    config.validar()

    def test_toml_valido_passa(self):
        self.toml('[dono]\nnome = "Ana"\n[agentes.faixas]\n"1" = "construtor"\n')
        config.validar()


class TestSegredoNoToml(unittest.TestCase):
    def test_permitidas(self):
        config._procurar_segredo({"caminhos": {"segredos": "/etc/krohnos"}})  # só o caminho da pasta passa
        for ruim in ({"painel": {"senha": "x"}}, {"caminhos": {"token": "x"}},
                     {"caminhos": {"segredos": {"senha": "x"}}}):
            with self.assertRaises(config.ConfigError, msg=str(ruim)):
                config._procurar_segredo(ruim)


class TestConfigLegado(ConfigBase):
    """Sem toml e sem env, hub.data()/home() reproduzem o comportamento de antes."""

    def legado(self, **pastas):
        """LEGADO apontando para pastas falsas (None = não existe)."""
        r = {}
        for chave in config.LEGADO:
            nome = chave.split(".")[1]
            r[chave] = str(self.dir / (nome if pastas.get(nome, True) else "nada"))
            if pastas.get(nome, True):
                (self.dir / nome).mkdir(exist_ok=True)
        return mock.patch.dict(config.LEGADO, r)

    def test_config_legado_vps_existe(self):
        with self.legado():
            self.assertEqual(hub.data(), self.dir / "dados")
            self.assertEqual(hub.home(), self.dir / "cofre")
            self.assertEqual(hub.codigo(), self.dir / "codigo")
            self.assertEqual(hub.segredos(), self.dir / "segredos")
            self.assertEqual(hub.home_agentes(), self.dir / "home_agentes")

    def test_config_legado_sem_vps_usa_pc(self):
        with self.legado(dados=False, cofre=False, codigo=False, segredos=False, home_agentes=False):
            self.assertEqual(hub.data(), hub.RAIZ / "data")
            self.assertEqual(hub.home(), Path.home() / ".nimbus")
            self.assertEqual(hub.codigo(), hub.RAIZ)
            self.assertEqual(hub.home_agentes(), Path.home())

    def test_config_legado_valores_de_hoje(self):
        self.assertEqual(config.LEGADO, {
            "caminhos.dados": "/var/lib/nimbus/reports", "caminhos.cofre": "/etc/nimbus/so",
            "caminhos.codigo": "/opt/nimbus/hub", "caminhos.segredos": "/etc/nimbus",
            "caminhos.home_agentes": "/home/nimbus"})

    def test_config_legado_apelidos(self):
        os.environ["CLAUDE_HOME"] = str(self.dir / "h")
        self.assertEqual(hub.home_agentes(), self.dir / "h")

    def test_conta_legado_sem_toml(self):
        from hub import conta
        self.assertEqual(conta.contas_config(), ("1", "2", "3", "4"))
        self.assertEqual(conta.faixa(1), "construtor")
        self.assertEqual(conta.faixa(4), "operador")  # hub.json

    def test_conta_toml_e_env(self):
        from hub import conta
        self.toml('[agentes]\ncontas = [1, 2]\n[agentes.faixas]\n"1" = "x"\n')
        self.assertEqual(conta.contas_config(), ("1", "2"))
        self.assertEqual(conta.faixa(1), "x")
        self.assertIsNone(conta.faixa(4))
        os.environ["KROHNOS_AGENTES_CONTAS"] = '["7"]'
        self.assertEqual(conta.contas_config(), ("7",))

    def test_painel_dados(self):
        from hub import painel
        os.environ["HUB_DATA"] = str(self.dir / "d")
        self.assertEqual(painel.dados(), self.dir / "d")
        del os.environ["HUB_DATA"]
        with self.legado(dados=False):  # VPS com /opt/.../hub mas sem a pasta de dados: caminho legado mesmo assim
            self.assertEqual(painel.dados(), self.dir / "nada")
        with self.legado(dados=False, codigo=False):
            self.assertEqual(painel.dados(), hub.RAIZ / "data")

    def test_hub_data_env_vale(self):
        os.environ["HUB_DATA"] = str(self.dir / "d")
        self.assertEqual(hub.data(), self.dir / "d")

    def test_toml_define_data(self):
        self.toml(f'[caminhos]\ndados = "{self.dir}/dados"\n')
        self.assertEqual(hub.data(), self.dir / "dados")


class TestIdentidade(ConfigBase):
    def setUp(self):
        super().setUp()
        for k in ("KROHNOS_DONO_NOME", "KROHNOS_GITHUB_ORG", "KROHNOS_WHATSAPP_REMETENTES"):
            os.environ.pop(k, None)

    def vps(self, existe=True):
        pasta = self.dir / "codigo"
        pasta.mkdir(exist_ok=True)
        return mock.patch.dict(config.LEGADO, {"caminhos.codigo": str(pasta if existe else self.dir / "nada")})

    def test_vps_sem_toml_rotinas_cron_e_o_crontab_do_repositorio(self):
        with self.vps(existe=True):
            self.assertEqual(config.get("rotinas.cron"), str(config.RAIZ / "deploy" / "nimbus.cron"))
        with self.vps(existe=False):
            self.assertEqual(config.get("rotinas.cron"), "")

    def test_neutro_sem_toml_fora_da_vps(self):
        with self.vps(existe=False):
            self.assertEqual(config.dono(), "dono")
            self.assertEqual(config.dono(True), "Dono")
            with self.assertRaises(config.ConfigError):
                config.repo("x")
            self.assertEqual(config.get("whatsapp.remetentes"), {})

    @unittest.skipUnless(ORG, "sem privado/legado-vps.json (snapshot): não há legado de org")
    def test_legado_vps_sem_toml_reproduz_hoje(self):
        with self.vps():
            self.assertEqual(config.dono(), "Fulano")
            self.assertEqual(config.repo("x"), f"{ORG}/x")
            self.assertEqual(config.get("whatsapp.remetentes"), {"fsite06": "crm", "asite01": "numero"})

    @unittest.skipUnless(ORG, "sem legado de org")
    def test_toml_e_env_vencem_o_legado(self):
        with self.vps():
            self.toml('[dono]\nnome = "Ana"\n')
            self.assertEqual(config.dono(), "Ana")
            self.assertEqual(config.get("github.org"), ORG)  # chave ausente preserva VPS antiga
            os.environ["KROHNOS_GITHUB_ORG"] = "org"
            self.assertEqual(config.repo("x"), "org/x")

    @unittest.skipUnless(ORG, "sem legado de org")
    def test_toml_antigo_preserva_integracoes_na_vps(self):
        with self.vps():
            self.toml('[caminhos]\ndados = "/tmp/dados"\n')
            self.assertEqual(config.repo("hub"), f"{ORG}/hub")
            self.assertEqual(config.get("whatsapp.remetentes"), {"fsite06": "crm", "asite01": "numero"})
            self.toml('[github]\norg = ""\n[whatsapp]\nremetentes = {}\n')
            self.assertNotEqual(config.get("github.org"), "evil")
            self.assertEqual(config.get("whatsapp.remetentes"), {})

    def test_remetentes_mescla_config_e_projetos(self):
        from hub import propostas
        self.toml('[whatsapp.remetentes]\nexistente = "crm"\n')
        projetos = self.dir / "projetos"
        projetos.mkdir()
        (projetos / "loja.json").write_text('{"remetentes": {"novo": "numero"}}')
        with mock.patch.object(propostas, "PASTA_PROJETOS", projetos):
            self.assertEqual(propostas.remetentes(), {"existente": "crm", "novo": "numero"})

    def test_negocios_vem_do_cadastro(self):
        from hub import sites, ui
        ids = [s["id"] for s in sites.ativos()]
        self.assertEqual([n for n, _ in ui.negocios()], ids)

    def test_remetentes_do_projeto_ou_da_config(self):
        from hub import propostas
        with self.vps(existe=False), mock.patch.object(propostas, "PASTA_PROJETOS", self.dir / "projetos"):
            self.assertEqual(propostas.remetentes(), {})
            (self.dir / "projetos").mkdir()
            (self.dir / "projetos" / "exemplo-clinica.json").write_text('{"remetentes": {"exemplo-clinica": "crm"}}')
            (self.dir / "projetos" / "exemplo-loja.json").write_text('{"remetentes": ["exemplo-loja"]}')
            self.assertEqual(propostas.remetentes(), {"exemplo-clinica": "crm", "exemplo-loja": "numero"})
            erros = propostas._erros_do_tipo("whatsapp", {"conteudo": "oi"}, {"remetente": "exemplo-clinica"})
            self.assertTrue(any("contato_id" in e for e in erros))

    def test_remetentes_ignora_projeto_malformado(self):
        from hub import propostas
        projetos = self.dir / "projetos"
        projetos.mkdir()
        (projetos / "a.json").write_text('{"remetentes": "asite01"}')
        (projetos / "b.json").write_text('{"remetentes": {"x": "invalido"}}')
        with self.vps(existe=False), mock.patch.object(propostas, "PASTA_PROJETOS", projetos):
            self.assertEqual(propostas.remetentes(), {})

    def test_vps_confere_tipo_do_remetente_legado(self):
        from hub import propostas
        projetos = self.dir / "projetos"
        projetos.mkdir()
        (projetos / "x.json").write_text('{"remetentes": {"fsite06": "numero"}}')
        with self.vps(existe=True), mock.patch.object(propostas, "PASTA_PROJETOS", projetos):
            erros = propostas._erros_do_tipo("whatsapp", {"conteudo": "oi"}, {"remetente": "fsite06", "numero": "5500000000001"})
            self.assertTrue(any("tipo diferente" in e for e in erros))

    @unittest.skipUnless(ORG, "sem legado de org")
    def test_migrar_grava_identidade_da_vps(self):
        saida = self.dir / "o.toml"
        os.environ["HUB_DATA"] = str(self.dir / "dados")  # o marcador do assistente não pode cair na pasta real
        config.main(["migrar", "--saida", str(saida), "--hub-json", str(self.dir / "x.json"),
                     "--painel-env", str(self.dir / "p.env")])
        os.environ["KROHNOS_CONFIG"] = str(saida)
        config.recarregar()
        self.assertEqual((config.dono(), config.get("github.org")), ("Fulano", ORG))
        self.assertEqual(config.get("whatsapp.remetentes"), {"fsite06": "crm", "asite01": "numero"})
        config.validar()


class TestMigrar(ConfigBase):
    def test_migrar_preserva_valores_e_nao_vaza_segredo(self):
        (self.dir / "hub.json").write_text(
            '{"mcp": "http://x:1/mcp", "faixas": {"1": "construtor", "2": "operador"},'
            ' "painel_modelos": ["a/b", "c/d"]}')
        (self.dir / "painel.env").write_text("PAINEL_SENHA=segredo-falso-123\nPAINEL_PORTA=9001\n")
        os.environ["HUB_DATA"] = str(self.dir / "dados")
        os.environ["NIMBUS_HOME"] = str(self.dir / "so")
        saida = self.dir / "out" / "krohnos.toml"
        rc = config.main(["migrar", "--saida", str(saida), "--hub-json", str(self.dir / "hub.json"),
                          "--painel-env", str(self.dir / "painel.env")])
        self.assertEqual(rc, 0)
        self.assertNotIn("segredo-falso-123", saida.read_text())
        for k in ("HUB_DATA", "NIMBUS_HOME"):
            del os.environ[k]
        os.environ["KROHNOS_CONFIG"] = str(saida)
        config.recarregar()
        self.assertEqual(config.get("caminhos.dados"), str(self.dir / "dados"))
        self.assertEqual(config.get("caminhos.cofre"), str(self.dir / "so"))
        self.assertEqual(config.get("agentes.faixas"), {"1": "construtor", "2": "operador"})
        self.assertEqual(config.get("agentes.contas"), ["1", "2"])
        self.assertEqual(config.get("agentes.modelos"), {"painel": ["a/b", "c/d"]})
        self.assertEqual(config.get("mcp.url"), "http://x:1/mcp")
        self.assertEqual(config.get("painel.porta"), 9001)
        config.validar()
        self.assertTrue((self.dir / "dados" / "comecar.concluido").is_file())  # VPS migrada nunca vê o assistente


if __name__ == "__main__":
    unittest.main()


class RegressaoRevisao(unittest.TestCase):
    def test_remetente_sem_executor_na_vps_e_recusado(self):
        from hub import propostas
        with mock.patch.object(propostas, "remetentes", return_value={"novo": "numero"}), mock.patch.object(Path, "is_dir", return_value=True):
            erros = propostas._erros_do_tipo("whatsapp", {"conteudo": "Oi"}, {"remetente": "novo", "numero": "5500000000005"})
        self.assertTrue(any("executor" in e for e in erros))

    def test_tipo_de_remetente_invalido_e_recusado(self):
        from hub import propostas
        with mock.patch.object(propostas, "remetentes", return_value={"novo": "desconhecido"}):
            erros = propostas._erros_do_tipo("whatsapp", {"conteudo": "Oi"}, {"remetente": "novo"})
        self.assertTrue(any("tipo de remetente" in e for e in erros))

    def test_alias_remetentes_acompanha_config(self):
        from hub import propostas
        with mock.patch.object(propostas, "remetentes", return_value={"loja": "numero"}):
            self.assertEqual(propostas.REMETENTES, {"loja": "numero"})

    def test_aviso_usa_nome_configurado(self):
        import subprocess, sys
        env = dict(os.environ, KROHNOS_DONO_NOME="Pessoa Exemplo")
        r = subprocess.run([sys.executable, "-c", "from hub import recibo; assert recibo.exige('aviso ao Pessoa Exemplo') == {'aviso'}; assert recibo.RE_FULANO is recibo.RE_DONO"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class RegressaoNegocios(ConfigBase):
    def test_bi_tem_config_independente_do_seo(self):
        os.environ.pop("KROHNOS_BI_NEGOCIOS", None)
        (self.dir / "nao-existe.toml").write_text("[bi.negocios]\nloja = 'Loja'\n")
        self.assertEqual(config.get("bi.negocios"), {"loja": "Loja"})

    def test_prompt_continua_compativel_com_executor_antigo(self):
        from hub import propostas
        texto = propostas.INSTRUCOES.format(tarefa="teste", codigo="/tmp/hub")
        self.assertNotIn("{dono}", texto)
        self.assertNotIn("{dono}", propostas.FRONTEIRA.replace("{codigo}", "/tmp/hub"))

    def test_coluna_existente_sobrevive_a_troca_do_dono(self):
        from hub import recibo
        consulta = mock.Mock()
        issue = {"team": {"states": {"nodes": [{"id": "anterior", "name": "Travada: precisa do Dono Antigo"}]}}}
        self.assertEqual(recibo.estado_travada(issue, consulta), "anterior")
        consulta.assert_not_called()


class GravarTest(ConfigBase):
    def setUp(self):
        super().setUp()
        self.dados = self.dir / "dados"
        os.environ["KROHNOS_CAMINHOS_DADOS"] = str(self.dados)
        self.sobre = self.dados / "krohnos.painel.toml"
        p = mock.patch.dict(config.LEGADO, {"caminhos.codigo": str(self.dir / "sem-vps")})  # a máquina de teste pode ser a VPS
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ)  # tests/__init__.py liga todos os plugins por env; aqui quem decide é o toml/sobreposição
        e.start()
        self.addCleanup(e.stop)
        os.environ.pop("KROHNOS_PLUGINS_LIGADOS", None)
        config.recarregar()

    def test_grava_na_sobreposicao_e_nao_toca_o_principal(self):
        texto = '# comentario do root\n[dono]\nnome = "Ana"\n\n[agentes]\ncontas = ["1", "2"]\n'
        arq = self.toml(texto)
        self.assertEqual(config.gravar({"agentes.faixas": {"1": "operador"}, "agentes.limite_usd_dia": 5}), self.sobre)
        self.assertEqual(arq.read_text(), texto)  # principal e comentários intactos
        self.assertEqual(config.get("dono.nome"), "Ana")
        self.assertEqual(config.get("agentes.contas"), ["1", "2"])
        self.assertEqual(config.get("agentes.faixas"), {"1": "operador"})
        self.assertEqual(config.get("agentes.limite_usd_dia"), 5)
        self.assertEqual(self.sobre.stat().st_mode & 0o777, 0o644)
        self.assertEqual([p.name for p in self.dados.iterdir() if p.name.endswith(".tmp")], [])

    def test_ataque_sobreposicao_nao_muda_plugins_caminho_nem_caminhos(self):
        # a sobreposição é gravável pelo painel/claude; o código do root não pode ser desviado por ela
        self.dados.mkdir(parents=True)
        self.sobre.write_text('[plugins]\ncaminho = "/tmp/x"\nligados = ["a"]\n[caminhos]\nsegredos = "/tmp/y"\ncodigo = "/tmp/z"\n'
                              '[cofre]\nlegado = true\n[github]\norg = "evil"\n', encoding="utf-8")
        config.recarregar()
        import io
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(config.get("plugins.caminho"), "")
            self.assertEqual(config.get("caminhos.segredos"), "/etc/krohnos")
            self.assertEqual(config.get("caminhos.codigo"), None)
            self.assertFalse(config.get("cofre.legado"))
            self.assertNotEqual(config.get("github.org"), "evil")
            from hub import plugins
            plugins.recarregar()
            self.assertEqual(plugins.caminhos(), [p for p in plugins.caminhos() if "/tmp/x" not in str(p)])
        self.assertEqual(config.get("plugins.ligados"), ["a"])  # a única que vale (ligar plugin já descoberto)
        for chave in ("plugins.caminho", "caminhos.segredos", "caminhos.codigo", "cofre.legado", "github.org"):
            self.assertIn(chave, err.getvalue())  # aviso por chave ignorada
        config.recarregar()

    def test_gravar_recusa_chave_fora_da_lista(self):
        for chave, v in (("plugins.caminho", "/tmp/x"), ("caminhos.segredos", "/tmp/y"), ("cofre.legado", True), ("github.org", "x"),
                         ("dono.nome", "x"), ("rotinas.cron", "/tmp/c")):
            with self.assertRaises(config.ConfigError, msg=chave):
                config.gravar({chave: v})
        self.assertFalse(self.sobre.exists())
        config.gravar({"rotinas.desligadas": ["a"], "plugins.ligados": ["x"]})
        self.assertEqual(config.get("rotinas.desligadas"), ["a"])
        self.assertEqual(config.SOBREPOSICAO_PERMITIDA, {"rotinas.desligadas", "agentes.contas", "agentes.faixas", "agentes.modelos",
                                                         "agentes.limites", "agentes.limite_usd_dia", "plugins.ligados"})

    def test_sem_sobreposicao_so_toml_e_env(self):
        config.gravar({"agentes.limite_usd_dia": 9, "plugins.ligados": ["a"]})
        self.assertEqual(config.get("agentes.limite_usd_dia"), 9)
        with config.sem_sobreposicao():
            self.assertEqual(config.get("agentes.limite_usd_dia"), 0)
            self.assertEqual(config.get("plugins.ligados"), [])
            with config.sem_sobreposicao():
                self.assertEqual(config.get("plugins.ligados"), [])
            self.assertEqual(config.get("plugins.ligados"), [])
        self.assertEqual(config.get("agentes.limite_usd_dia"), 9)

    def test_precedencia_env_sobreposicao_principal_padrao(self):
        self.toml('[agentes]\nlimite_usd_dia = 1\ncontas = ["1", "2"]\n')
        self.assertEqual(config.get("agentes.limite_usd_dia"), 1)  # principal
        config.gravar({"agentes.limite_usd_dia": 2})
        self.assertEqual(config.get("agentes.limite_usd_dia"), 2)  # sobreposição vence o principal
        self.assertEqual(config.get("agentes.contas"), ["1", "2"])  # chave que ela não tem vem do principal
        os.environ["KROHNOS_AGENTES_LIMITE_USD_DIA"] = "3"
        self.assertEqual(config.get("agentes.limite_usd_dia"), 3)  # env vence tudo
        del os.environ["KROHNOS_AGENTES_LIMITE_USD_DIA"]
        self.sobre.unlink()
        self.assertEqual(config.get("agentes.limite_usd_dia"), 1)  # sem sobreposição: como hoje
        os.environ["KROHNOS_CONFIG"] = str(self.dir / "nao-existe.toml")
        config.recarregar()
        self.assertEqual(config.get("agentes.limite_usd_dia"), 0)  # padrão

    def test_sem_sobreposicao_e_como_hoje(self):
        self.toml('[dono]\nnome = "Ana"\n')
        self.assertFalse(self.sobre.exists())
        self.assertEqual(config.get("dono.nome"), "Ana")

    def test_outro_processo_ve_a_gravacao_sem_recarregar(self):
        self.assertEqual(config.get("agentes.limite_usd_dia"), 0)
        self.dados.mkdir()
        self.sobre.write_text("[agentes]\nlimite_usd_dia = 7\n")  # outro processo gravou
        self.assertEqual(config.get("agentes.limite_usd_dia"), 7)

    def test_principal_somente_leitura_grava_mesmo_assim(self):
        pasta = self.dir / "etc"
        pasta.mkdir()
        arq = pasta / "krohnos.toml"
        arq.write_text('[dono]\nnome = "Ana"\n')
        arq.chmod(0o444)
        pasta.chmod(0o555)
        self.addCleanup(lambda: pasta.chmod(0o755))
        os.environ["KROHNOS_CONFIG"] = str(arq)
        config.recarregar()
        config.gravar({"agentes.limite_usd_dia": 4})
        self.assertEqual(config.get("agentes.limite_usd_dia"), 4)
        self.assertEqual(arq.read_text(), '[dono]\nnome = "Ana"\n')

    def test_so_chaves_conhecidas(self):
        for chave in ("agentes.nao_existe", "secao.x", "semponto"):
            with self.assertRaises(config.ConfigError):
                config.gravar({chave: 1})
        self.assertFalse(self.sobre.exists())

    def test_recusa_segredo(self):
        config.gravar({"agentes.limite_usd_dia": 1})
        antes = self.sobre.read_text()
        with self.assertRaises(config.ConfigError):
            config.gravar({"bi.negocios": {"api_key": "x"}})
        self.assertEqual(self.sobre.read_text(), antes)

    def test_sobreposicao_corrompida_nao_derruba(self):
        self.dados.mkdir()
        self.sobre.write_text("isto = [nao fecha\n")
        self.assertEqual(config.get("agentes.limite_usd_dia"), 0)  # leitura ignora
        config.gravar({"agentes.limite_usd_dia": 3})
        self.assertEqual(config.get("agentes.limite_usd_dia"), 3)
        self.assertTrue((self.dados / "krohnos.painel.toml.corrompido").exists())

    def test_principal_corrompido_nao_impede_gravar(self):
        self.toml("isto = [nao fecha\n")
        config.gravar({"agentes.limite_usd_dia": 3})
        self.assertTrue(self.sobre.exists())

    def test_modo_existente_e_preservado(self):
        config.gravar({"agentes.limite_usd_dia": 1})
        self.sobre.chmod(0o640)
        config.gravar({"agentes.limite_usd_dia": 2})
        self.assertEqual(self.sobre.stat().st_mode & 0o777, 0o640)

    def test_falha_na_escrita_nao_toca_o_original(self):
        config.gravar({"agentes.limite_usd_dia": 1})
        antes = self.sobre.read_text()
        with mock.patch("os.replace", side_effect=OSError("disco")):
            with self.assertRaises(OSError):
                config.gravar({"agentes.limite_usd_dia": 9})
        self.assertEqual(self.sobre.read_text(), antes)
        self.assertEqual([p.name for p in self.dados.iterdir() if p.name.endswith(".tmp")], [])

    def test_gravacoes_concorrentes_sem_perda(self):
        import threading
        erros = []

        def soma(n):
            try:
                for i in range(15):
                    config.gravar(lambda: {"agentes.limites": {**config.get("agentes.limites"), f"{n}{i}": 1}})
            except Exception as e:  # noqa: BLE001
                erros.append(e)
        ts = [threading.Thread(target=soma, args=(n,)) for n in "abcd"]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(erros, [])
        self.assertEqual(len(config.get("agentes.limites")), 60)

    def test_gravacoes_concorrentes_entre_processos(self):
        import subprocess, sys
        codigo = ("from hub import config\nimport sys\nfor i in range(12):\n"
                  " config.gravar(lambda: {'agentes.limites': {**config.get('agentes.limites'), sys.argv[1]+str(i): 1}})")
        ps = [subprocess.Popen([sys.executable, "-c", codigo, n], env=dict(os.environ), cwd=str(Path(config.__file__).parent.parent))
              for n in "xyz"]
        self.assertEqual([p.wait() for p in ps], [0, 0, 0])
        config.recarregar()
        self.assertEqual(len(config.get("agentes.limites")), 36)
