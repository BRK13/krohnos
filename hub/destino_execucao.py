"""Portão de destino de execução (caso/Pedido 940): o Pedido `vps` que roda um script de coleta ou giro
numa máquina que não é a produtora dele é recusado ao ser criado.

O root do hub roda `dados.comandos` na servidor_hub, salvo `ssh <máquina>` no próprio comando (ou `dados.host`). O Pedido 940
copiava `coleta/` e rodava o giro do TRF2-TR na servidor_hub; lá o `vps-tjs.sh` não tem cron (linhas comentadas em
`coleta/vps-cron.txt`) e o diário dos tribunais é da vps-servidor_b (`vps-diario-tribunais.sh`): rodar na hub não prova a
saída 4 onde ela vale e grava no banco a partir da máquina errada.

`PRODUTORES` é o mapa script → máquina do cron versionado (clienteh: `coleta/vps-gemea-cron.txt` = vps-servidor_b,
`coleta/vps-cron.txt` = a servidor_a de antes, hoje o dedicado). Script de coleta novo entra aqui; sem entrada, o portão não opina.

desde 22/10 a servidor_a é o servidor dedicado, a mesma máquina em que o root do hub roda. `vps-servidor_a` (e os apelidos
`servidor_a`, `vps-nova`, `dedicado`) resolve para o HOST_PADRAO, que roda local: o `ssh vps-servidor_a` do Pedido 1190 morreu em
"Could not resolve hostname" e a servidor_a velha está parada. Nenhum destino resolve mais para `vps-servidor_a`.
"""
import re

from hub.comum import maquinas

HOST_PADRAO = "servidor_hub"  # onde o root do hub roda os comandos (o dedicado, desde 22/10): bash local, sem ssh
ALIAS = {"vps-velha": HOST_PADRAO, "hub": HOST_PADRAO, "vps-servidor_a": HOST_PADRAO, "vps-nova": HOST_PADRAO, "servidor_a": HOST_PADRAO,
         "dedicado": HOST_PADRAO, "vps-dedicado": HOST_PADRAO, "servidor_b": "vps-servidor_b"}

# (caso): a chave temporária do conector foi pedida com o insert apontando para o IP da servidor_a velha,
# parada desde a virada para o dedicado. Escrita em tabela de credencial num servidor aposentado grava a chave onde ninguém
# lê: recusa, sem redirecionar sozinho (quem pediu refaz o Pedido com o destino certo). Aposentado = IP de `maquinas.PADRAO`
# que nenhuma máquina ativa (`maquinas.json`) usa mais; em produção `servidor_a` já aponta para o dedicado, então só o IP antigo vale.
ESCRITA_CREDENCIAL = re.compile(
    r"\b(?:insert\s+into|update|delete\s+from)\s+(?:\"?\w+\"?\.)?\"?(?:chave_api|api_keys?|apikeys?|credenciais?|tokens?|segredos?)\b",
    re.I)

PRODUTORES = {
    "vps-diario-tribunais.sh": "vps-servidor_b",  # coleta/vps-gemea-cron.txt: 5 3-8 * * *
    "vps-tjs.sh": "vps-servidor_b",  # na hub/servidor_a as linhas do vps-cron.txt estão comentadas; o diário chama o `incremental`
    "vps-djen-stm.sh": HOST_PADRAO,  # o DJEN dá 403 ao IP da gêmea; era a servidor_a, hoje o dedicado
}

_SSH = re.compile(r"(?:^|[;&|]\s*|\s)ssh\s+(?:-\S+\s+(?:\S+\s+)?)*(?:\w+@)?(vps-[\w-]+|hub|prec[12]|dedicado)\b")


def resolver(host):
    """Nome canônico da máquina (apelido → HOST_PADRAO ou vps-servidor_b); nome desconhecido volta como veio."""
    h = str(host or "").strip() or HOST_PADRAO
    return ALIAS.get(h, h)


def _comandos(texto):
    """Quebra o texto em comandos (linha, `;`, `&&`, `||`, `|`, `&`) fora de aspas: `ssh X 'a && b'` fica inteiro."""
    out, atual, aspas, i = [], "", "", 0
    while i < len(texto):
        c = texto[i]
        if aspas:
            aspas = "" if c == aspas else aspas
        elif c in "'\"":
            aspas = c
        elif c in "\n;&|":
            out.append(atual)
            atual = ""
            i += 1
            continue
        atual += c
        i += 1
    return [x for x in out + [atual] if x.strip()]


def _host_padrao(dados):
    return resolver(dados.get("host"))


def _ips_aposentados():
    """IP antigo da servidor_a, a menos que alguma máquina ativa de `maquinas.json` tenha voltado a usá-lo."""
    return {maquinas.PADRAO["servidor_a"]} - {maquinas.ip(n) for n in ("servidor_a", "servidor_b", "dedicado")}


def _aposentada_citada(comandos):
    """IP de servidor aposentado citado em qualquer parte do Pedido (ssh, psql -h, heredoc), ou ''."""
    for ip in sorted(_ips_aposentados()):
        if re.search(rf"(?<![\w.-]){re.escape(ip)}(?![\w-]|\.\d)", comandos):
            return ip
    return ""


def _erros_credencial(comandos):
    if not ESCRITA_CREDENCIAL.search(comandos):
        return []
    ip = _aposentada_citada(comandos)
    if not ip:
        return []
    return [f"vps: escrita em tabela de credencial com destino em {ip}, servidor aposentado (produção está no dedicado); refaça "
            f"o Pedido apontando para o dedicado. O hub não redireciona sozinho"]


def erros(dados):
    """Mensagens de recusa (lista vazia = pode seguir) para um Pedido vps. Cada comando vale pela máquina em que ele
    próprio roda: o `ssh <máquina>` do comando, senão `dados.host`, senão a servidor_hub (um `ssh vps-servidor_b true` numa linha
    não manda para lá o script da linha seguinte)."""
    out = _erros_credencial(str(dados.get("comandos") or ""))
    for cmd in _comandos(str(dados.get("comandos") or "")):
        m = _SSH.search(cmd)
        if m and m.group(1) != HOST_PADRAO and resolver(m.group(1)) == HOST_PADRAO:  # ssh para a própria máquina
            msg = (f"vps: `ssh {m.group(1)}` não resolve no root do hub: desde 22/10 a servidor_a é o dedicado, a máquina onde o "
                   f"Pedido já roda; tire o ssh e rode o comando direto (ou dados.host omitido)")
            if msg not in out:
                out.append(msg)
        host = resolver(m.group(1)) if m else _host_padrao(dados)
        for script, produtor in PRODUTORES.items():
            if re.search(rf"(?<![\w.-]){re.escape(script)}(?![\w.-])", cmd) and host != produtor:
                msg = (f"vps: {script} é da {produtor} (cron e banco de lá), não da {host}; refaça o Pedido com destino à "
                       f"{produtor}, no /opt/clienteh (dados.host ou ssh {produtor})")
                if msg not in out:
                    out.append(msg)
    return out
