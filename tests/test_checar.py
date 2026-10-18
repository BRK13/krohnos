"""Portão de segredo (scripts/checar.sh): sem rede, só com gitleaks já disponível."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _gitleaks():
    g = os.environ.get("GITLEAKS")
    if g and os.access(g, os.X_OK):
        return g
    g = shutil.which("gitleaks")
    if g:
        return g
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kronos/gitleaks-8.30.1/gitleaks"
    return str(cache) if os.access(cache, os.X_OK) else None


GITLEAKS = _gitleaks()


PEM = ("-----BEGIN " + "RSA PRIVATE KEY-----\n" + "MIIEowIBAAKCAQEA" + "x7Qp" * 20
       + "\n-----END " + "RSA PRIVATE KEY-----\n")


class TestTextoDosScripts(unittest.TestCase):
    def test_checar_usa_redact_e_config(self):
        t = (RAIZ / "scripts/checar.sh").read_text()
        self.assertIn("--redact", t)
        self.assertIn(".gitleaks.toml", t)

    def test_instalador_fixa_versao_e_sha(self):
        t = (RAIZ / "scripts/gitleaks.sh").read_text()
        self.assertIn("8.30.1", t)
        self.assertIn("551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb", t)

    def test_opcao_invalida_sai_2(self):
        r = subprocess.run([str(RAIZ / "scripts/checar.sh"), "--bogus"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    # test_atualizar_chama_checar, test_atualizar_so_aborta_em_achado, test_atualizar_cria_pasta_do_painel_antes_dos_testes moram em tests/test_checar_deploy.py
    #: leem deploy/atualizar.sh, o deploy da nossa VPS; fora do snapshot público.

    def test_gitleaks_indisponivel_sai_3(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "scripts").mkdir()
            shutil.copy(RAIZ / "scripts/checar.sh", Path(d) / "scripts/checar.sh")
            falso = Path(d) / "scripts/gitleaks.sh"
            falso.write_text("#!/usr/bin/env bash\necho 'sem rede' >&2\nexit 1\n")
            falso.chmod(0o755)
            r = subprocess.run([str(Path(d) / "scripts/checar.sh"), "--sem-testes"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("gitleaks indisponível", r.stderr)


@unittest.skipUnless(GITLEAKS, "gitleaks 8.30.1 indisponível (passe GITLEAKS=<caminho>)")
class TestGitleaksNaArvore(unittest.TestCase):
    def _varrer(self, pasta):
        return subprocess.run(
            [GITLEAKS, "dir", str(pasta), "--no-banner", "--redact", "--config", str(RAIZ / ".gitleaks.toml")],
            capture_output=True, text=True)

    def test_segredo_plantado_acusa_sem_vazar(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "chave.pem").write_text(PEM)
            r = self._varrer(d)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("x7Qp" * 5, r.stdout + r.stderr)

    def test_sem_segredo_passa(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "ola.py").write_text('print("olá")\n')
            r = self._varrer(d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


@unittest.skipUnless(GITLEAKS, "gitleaks 8.30.1 indisponível (passe GITLEAKS=<caminho>)")
@unittest.skipUnless((RAIZ / "scripts/checar_publico.py").exists(), "--so-arvore é do repo privado (checar_publico.py fica fora do público)")
class TestArvoreDoGit(unittest.TestCase):
    """checar.sh --so-arvore: só vale o que o git considera do projeto (.gitignore respeitado)."""

    def _repo(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        subprocess.run(["git", "init", "-q", d.name], check=True)
        (Path(d.name) / ".gitignore").write_text(".env\n")
        return Path(d.name)

    def _checar(self, repo):
        env = dict(os.environ, GITLEAKS=GITLEAKS)
        return subprocess.run([str(RAIZ / "scripts/checar.sh"), "--so-arvore", str(repo)],
                              capture_output=True, text=True, env=env)

    def test_arquivo_ignorado_nao_acusa(self):
        repo = self._repo()
        (repo / ".env").write_text(PEM)
        r = self._checar(repo)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_arquivo_novo_nao_ignorado_acusa(self):
        repo = self._repo()
        (repo / "chave.pem").write_text(PEM)
        r = self._checar(repo)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("x7Qp" * 5, r.stdout + r.stderr)

    def test_pasta_sem_git_falha(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "ola.py").write_text("x = 1\n")
            r = self._checar(d)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("checar: ok", r.stdout)

    def test_caminho_relativo_resolvido_no_cwd(self):
        repo = self._repo()
        (repo / "ola.py").write_text("x = 1\n")
        env = dict(os.environ, GITLEAKS=GITLEAKS)
        r = subprocess.run([str(RAIZ / "scripts/checar.sh"), "--so-arvore", repo.name],
                           capture_output=True, text=True, env=env, cwd=repo.parent)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("checar: ok", r.stdout)

    def test_arquivo_rastreado_acusa(self):
        repo = self._repo()
        (repo / "chave.pem").write_text(PEM)
        subprocess.run(["git", "-C", str(repo), "add", "chave.pem"], check=True)
        self.assertNotEqual(self._checar(repo).returncode, 0)


if __name__ == "__main__":
    unittest.main()
