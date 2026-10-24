"""IP das máquinas do ClienteJ num lugar só (plano do servidor dedicado, Task 2).

Lê /etc/nimbus/maquinas.json ({"servidor_a": "...", "servidor_b": "...", "dedicado": "...", "vps8238": "..."}); sem o arquivo, chave
ausente ou valor vazio, vale o padrão de hoje. O `dedicado` não tem padrão: só existe depois do D0.
Quando uma máquina mudar de IP, o arquivo muda; nenhum outro lugar do hub guarda o endereço.
"""
import functools
import json
import subprocess
from pathlib import Path

ARQUIVO = Path("/etc/nimbus/maquinas.json")
PADRAO = {"servidor_a": "192.0.2.1", "servidor_b": "192.0.2.2", "vps8238": "192.0.2.3"}
NOMES = ("servidor_a", "servidor_b", "dedicado", "vps8238")  # servidor_b = gêmea; vps8238 = a servidor_hub antiga (asite01/fsite06 até a)


def _ler(arquivo):
    try:
        dados = json.loads(Path(arquivo).read_text())
    except (OSError, ValueError):
        return {}
    return dados if isinstance(dados, dict) else {}


def ip(nome, arquivo=None):
    """IP da máquina `nome` (servidor_a, servidor_b, dedicado, vps8238). KeyError se o nome não existe ou não tem IP."""
    if nome not in NOMES:
        raise KeyError(nome)
    valor = _ler(arquivo or ARQUIVO).get(nome)
    if isinstance(valor, str) and valor.strip():
        return valor.strip()
    return PADRAO[nome]


@functools.lru_cache(maxsize=1)
def _ips_locais():
    try:
        return frozenset(subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split())
    except (OSError, subprocess.SubprocessError):
        return frozenset()


def ssh_para(nome, opcoes=("-i", "/root/.ssh/clientek", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                            "-o", "ServerAliveInterval=30"), locais=None, arquivo=None):
    """Prefixo `ssh root@<ip>` para a máquina `nome`, ou None (roda local, `hub.apps._rodar`) se o IP é desta máquina.
    desde 22/10 a servidor_a é o dedicado, onde o hub roda; o ssh do root para o próprio IP morria em "Identity file
    /root/.ssh/clientek not accessible" e "Host key verification failed" (avisos-banco, bi-negocio)."""
    alvo = ip(nome, arquivo)
    if alvo in (_ips_locais() if locais is None else locais):
        return None
    return ["ssh", *opcoes, "root@" + alvo]
