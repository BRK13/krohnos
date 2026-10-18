"""Leitura do código atual dos repositórios dos sites, direto do origin (git fetch raso num clone nu em cache).

Cache em $HUB_DATA/.repos/<dono>__<repo>.git. Credencial do GitHub, nesta ordem: GH_TOKEN/GITHUB_TOKEN,
$NIMBUS_HOME/credenciais/github.json ({"token": ...}), `gh auth token` e, na VPS, o GH_TOKEN do usuário claude
(.claude-token.env na home das contas). O token vai para o git por variável de ambiente, nunca na linha de comando.
"""
import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from hub import data, home, home_agentes

TOKEN_CLAUDE_VPS = home_agentes() / ".claude-token.env"
LIMITE_ARQUIVO = 400_000


class SemAcesso(RuntimeError):
    """Não deu para buscar o repositório (sem credencial, sem rede ou ref inexistente)."""


def token_github():
    for nome in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(nome):
            return os.environ[nome]
    try:
        return json.loads((home() / "credenciais/github.json").read_text(encoding="utf-8"))["token"]
    except (OSError, ValueError, KeyError):
        pass
    if shutil.which("gh"):
        try:
            r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        m = re.search(r"^\s*(?:export\s+)?GH_TOKEN=['\"]?([^'\"\s]+)", TOKEN_CLAUDE_VPS.read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


class Repo:
    """Um branch do origin, lido sem checkout: `grep` lista caminhos, `ler` devolve o texto."""

    def __init__(self, github, base, url=None, cache=None):
        self.github, self.base = github, base
        self.url = url or f"https://github.com/{github}.git"
        self.dir = Path(cache or data() / ".repos") / (github.replace("/", "__") + ".git")
        self.ref = f"refs/heads/{base}"
        self.sha = None

    def _git(self, *args, timeout=120, env=None):
        r = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(self.dir), *args], capture_output=True,
                           timeout=timeout, env=env)
        if r.returncode not in (0, 1):  # 1 = grep sem resultado
            raise SemAcesso(f"git {args[0]} em {self.github}: {r.stderr.decode('utf-8', 'ignore').strip()[-200:]}")
        return r.stdout.decode("utf-8", "ignore")

    def buscar(self):
        """git fetch raso do branch base. Levanta SemAcesso se não der."""
        if not shutil.which("git"):
            raise SemAcesso("git não instalado")
        self.dir.mkdir(parents=True, exist_ok=True)
        if not (self.dir / "HEAD").exists():
            self._git("init", "--bare", "-q")
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        token = token_github() if self.url.startswith("https://github.com/") else None
        if token:
            cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                       GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {cred}")
        try:
            r = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(self.dir), "fetch", "-q", "--depth", "1",
                                "--no-tags", self.url, f"+{self.ref}:{self.ref}"], capture_output=True, timeout=600, env=env)
        except subprocess.TimeoutExpired:
            raise SemAcesso(f"git fetch de {self.github} demorou demais") from None
        if r.returncode != 0:
            erro = r.stderr.decode("utf-8", "ignore").strip().splitlines()
            raise SemAcesso(f"git fetch de {self.github} ({self.base}) falhou: {(erro or ['?'])[-1][:200]}"
                            + ("" if token else "; sem token do GitHub aqui"))
        self.sha = self._git("rev-parse", self.ref).strip()
        # Clone raso: o commit anterior fica solto a cada fetch; limpa para o cache não crescer.
        self._git("reflog", "expire", "--expire=now", "--all")
        self._git("gc", "--prune=now", "-q", timeout=300)
        return self

    def grep(self, *padroes, ignorar_caixa=False, palavra=False):
        """Caminhos (texto, não binário) que casam com algum dos padrões (regex estendida do git)."""
        args = ["grep", "-I", "-l", "-E"] + (["-i"] if ignorar_caixa else []) + (["-w"] if palavra else [])
        for p in padroes:
            args += ["-e", p]
        saida = self._git(*args, self.ref, "--")
        return sorted({linha.split(":", 1)[1] for linha in saida.splitlines() if ":" in linha})

    def ler(self, caminho):
        return self._git("show", f"{self.ref}:{caminho}")[:LIMITE_ARQUIVO]
