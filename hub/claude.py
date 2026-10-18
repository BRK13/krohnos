"""Claude da VPS (assinatura Max do usuário `claude`) para o trabalho interno do hub, no lugar do OpenRouter.

    from hub import claude
    texto = claude.perguntar(prompt, modelo="sonnet", nome="aio-analise")

Roda `claude -p` sem ferramentas, sem MCP e sem sessão, com o prompt pela entrada padrão. Como root (cron do hub),
troca para o usuário claude com `runuser` e ambiente limpo: nenhuma credencial do hub chega ao processo do Claude.
Segue as regras da conta (hub/conta.py): uma chamada por vez com a trava da conta, registro em uso.jsonl e respeito à
pausa por limite. Só para trabalho interno do dono; o que atende cliente ou visitante continua na API.
Failover (dono, 15/10: "estourou, passa pra outra conta"): contas 1→2→3→4 com login e sem pausa; o limite
pausa só a conta que bateu. Com todas fora, as reservas de API em lista (hub.json → reservas_api, na ordem;; o Haiku
começa pela mensal), cada uma com teto de conta.bloqueio_reserva e teto por chamada. ContaPausada só quando nem a reserva pode.
CLAUDE_BIN muda o caminho do executável (testes, PC).
"""
import contextlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from hub import conta, home_agentes

BIN = os.environ.get("CLAUDE_BIN") or str(home_agentes() / ".local/bin/claude")
USUARIO = "claude"
SISTEMA = "Você é um analista de SEO que responde em português do Brasil, direto, só com o que foi pedido."
ESPERA_TRAVA = 3 * 3600  # o lote de domingo espera a vez atrás das tarefas da fila
RE_SEM_SALDO = re.compile(r"invalid.{0,20}api.?key|credit balance|billing|credit.{0,30}(low|exhaust|insufficient|depleted)|"
                          r"insufficient.{0,20}(credit|fund|balanc)", re.I)
ORCAMENTO_API_USD = 5  # por chamada na reserva de API (--max-budget-usd), o mesmo teto por tarefa da agenda


class ContaPausada(RuntimeError):
    pass


class _Limite(Exception):
    """A conta respondeu limite: tenta a próxima."""


class _SemSaldo(Exception):
    """A reserva de API recusou por chave ou crédito: sai da escolha e tenta a próxima."""


class ContaOcupada(RuntimeError):
    """A trava da conta não liberou dentro de espera_trava."""


def _root():
    return getattr(os, "geteuid", lambda: -1)() == 0


def comando(modelo, sistema, n="1", reserva=None, leitura=()):
    """`leitura`: pastas que o Claude pode LER com a ferramenta Read (imagens de um post, por exemplo); sem ela, nenhuma ferramenta."""
    cmd = [BIN, "-p", "--model", modelo, "--tools", "Read" if leitura else "", "--strict-mcp-config", "--no-session-persistence",
           "--system-prompt", sistema, "--output-format", "json"]
    for pasta in leitura:
        cmd += ["--add-dir", str(pasta)]
    if n == conta.API:
        cmd += ["--max-budget-usd", str(ORCAMENTO_API_USD)]
    if _root():
        if n == conta.API:  # a chave sai do arquivo já como o usuário claude: nunca passa pela linha de comando
            cmd = ["sh", "-c", 'ANTHROPIC_API_KEY="$(cat "$0")" exec "$@"', str(conta.chave_api_arquivo(reserva))] + cmd
        dir_conta = [f"CLAUDE_CONFIG_DIR=/home/{USUARIO}/.claude{conta.sufixo(n)}"] if n != "1" else []
        cmd = ["runuser", "-u", USUARIO, "--", "env", "-i", f"HOME=/home/{USUARIO}", "LANG=C.UTF-8",
               f"PATH=/home/{USUARIO}/.local/bin:/usr/local/bin:/usr/bin:/bin", "CLAUDE_CODE_MAX_OUTPUT_TOKENS=32000",
               *dir_conta] + cmd
    return cmd


def _env(n, reserva=None):
    if _root():
        return None
    env = {**os.environ, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000"}
    if n != "1":
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)  # o token da conta 1 venceria o CLAUDE_CONFIG_DIR e a chave de API
        env["CLAUDE_CONFIG_DIR"] = str(Path(os.environ.get("CLAUDE_HOME", f"/home/{USUARIO}")) / f".claude{conta.sufixo(n)}")
    if n == conta.API:
        env["ANTHROPIC_API_KEY"] = conta.chave_api(reserva) or ""
    return env


def _trava(espera=ESPERA_TRAVA, n="1"):
    """A trava da conta onde ela existe (VPS); no PC, nos testes e na reserva de API, nenhuma."""
    if n == conta.API or os.name != "posix" or not os.path.isdir(os.path.dirname(conta.TRAVA)):
        return contextlib.nullcontext(True)
    return conta.trava(esperar=espera, caminho=conta.caminho_trava(n))


def _trava_reserva(r, espera):
    if os.name != "posix" or not os.path.isdir(os.path.dirname(conta.TRAVA)):
        return contextlib.nullcontext(True)
    return conta.trava(esperar=espera, caminho=conta.TRAVA.replace(".lock", f"-api-{r['id']}.lock"))


def _nas_reservas(so_mensal, prompt, modelo, sistema, timeout, nome, base_dados, avisar_fn, espera_trava, leitura=()):
    """Tenta as reservas de API na ordem da lista; crédito recusado ou limite passa para a próxima.
    Devolve (texto, None) ou (None, motivo)."""
    motivo = conta.bloqueio_api(base_dados)
    if motivo:
        return None, motivo
    motivo, tentadas = "a API também respondeu limite", set()
    while r := next((x for x in conta.reservas() if x["id"] not in tentadas and (x["mensal"] or not so_mensal)
                     and not conta.bloqueio_reserva(x, base_dados)), None):
        tentadas.add(r["id"])
        try:
            # a trava da reserva serializa as chamadas e o orçamento é revalidado depois da espera: Haiku concorrente
            # não escolhe o mesmo saldo e estoura o teto do dia ou do mês
            with _trava_reserva(r, espera_trava) as pegou:
                if not pegou or conta.bloqueio_reserva(r, base_dados):
                    continue
                return _perguntar_na(conta.API, prompt, modelo, sistema, timeout, nome, base_dados, avisar_fn,
                                     espera_trava, r, leitura), None
        except (_Limite, _SemSaldo):
            continue
    return None, motivo


def perguntar(prompt, modelo="sonnet", sistema=SISTEMA, timeout=1200, nome="hub", base_dados=None, avisar_fn=None,
              espera_trava=ESPERA_TRAVA, permitir_api=True, leitura=()):
    args = (prompt, modelo, sistema, timeout, nome, base_dados, avisar_fn, espera_trava, tuple(leitura))
    if permitir_api and modelo == "haiku":  # trabalho pequeno gasta primeiro o crédito mensal use-ou-perca e poupa as contas OAuth
        texto, _ = _nas_reservas(True, *args)
        if texto is not None:
            return texto
    for n in conta.com_login():
        if conta.indisponivel(n, base_dados):  # pausa ou janela de 5 h esgotada: segue sem gastar uma chamada que falharia
            continue
        try:
            return _perguntar_na(n, *args[:8], leitura=args[8])
        except _Limite:
            continue
    if not permitir_api:
        raise ContaPausada("contas da assinatura indisponíveis; reserva paga desabilitada")
    texto, motivo = _nas_reservas(False, *args)
    if texto is not None:
        return texto
    raise ContaPausada(f"contas Claude no limite de uso e reserva de API fora ({motivo})")


def _perguntar_na(n, prompt, modelo, sistema, timeout, nome, base_dados, avisar_fn, espera_trava, reserva=None, leitura=()):
    with _trava(espera_trava, n) as pegou:
        if not pegou:
            raise ContaOcupada(f"conta Claude {n} ocupada: a trava não liberou em {espera_trava} s")
        inicio = conta.agora()
        r = subprocess.run(comando(modelo, sistema, n, reserva, leitura), input=prompt, capture_output=True, text=True, encoding="utf-8",
                           timeout=timeout, cwd=tempfile.gettempdir(), env=_env(n, reserva))
        fim = conta.agora()
    try:
        d = json.loads(r.stdout)
    except ValueError:
        d = None
    texto = str((d or {}).get("result") or "")
    ok = d is not None and not d.get("is_error") and bool(texto.strip())
    bruto = texto or r.stderr or r.stdout or ""
    limite = not ok and (conta.eh_limite(bruto) or (d or {}).get("api_error_status") == 429)
    with contextlib.suppress(OSError):
        conta.registrar({"inicio": conta.iso(inicio), "fim": conta.iso(fim), "origem": "hub", "conta": n, "nome": nome,
                         **({"reserva": reserva["id"]} if reserva else {}), "modelo": modelo, "ok": ok, "limite": limite, "custo_usd": (d or {}).get("total_cost_usd"),
                         "tokens": conta.tokens_de(d), "turnos": (d or {}).get("num_turns"),
                         "duracao_s": round((fim - inicio).total_seconds(), 1),
                         "erro": None if ok else bruto.strip()[-300:], "saida": None}, base_dados)
    if reserva and not ok and ((d or {}).get("api_error_status") in (401, 402, 403) or RE_SEM_SALDO.search(bruto)):
        conta.marcar_sem_saldo(reserva, base_dados)  # a chave sai da escolha; quem chamou tenta a próxima reserva
        raise _SemSaldo(f"reserva {reserva['id']} sem saldo")
    if limite:
        if n != conta.API:  # a reserva não pausa: o teto dela é o de gasto
            conta.pausar(bruto, f"hub {nome}", base_dados, avisar_fn=avisar_fn, n=n)
        raise _Limite(bruto.strip()[:200])
    if not ok:
        raise RuntimeError(f"claude saiu com {r.returncode}: {bruto.strip()[:300]}")
    return texto
