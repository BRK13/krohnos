"""Revisão do Claude nos PRs do executor Codex (branch `codex/`), cron do claude a cada 10 min.

O codex-revisar (orch) pula esses PRs: quem escreveu não revisa. Um comentário por commit, só depois de publicado
grava o feito em $HUB_DATA/claude/revisoes-claude.txt (e uma linha em revisoes-claude.jsonl).
"""
import json
import subprocess
import sys

from hub import config, claude, conta, github_api

REPOS = "clienteh pereiraos-privado triagem-wpp cliente-c cliente-a-v2 clienteipsi-v2 psite13 infra".split()
CORTE = 150_000
SISTEMA = "Você é um revisor de código que responde em português do Brasil, direto, só com o que foi pedido."
PEDIDO = ("Revise só o que este PR muda (o diff está abaixo, não rode comandos). Aponte bug real, quebra de produção, "
          "dado perdido, segredo exposto e teste que não testa. Português, curto, cada achado com arquivo:linha. "
          "Sem achado sério, responda só: Sem achados sérios.")


def _gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", check=True, timeout=300).stdout


def _abertos(repo):
    """PRs abertos pela REST medida (consumidor revisar_codex, cache de 60 s, ETag); o `gh pr list` gastava 1 chamada fixa por repo a cada 10 min."""
    pulls = []
    for pagina in range(1, 6):  # até 500 PRs; antes eram 200 (2 páginas) e mais de 100 abertos sumiam calado
        lote = github_api.get(f"repos/{config.repo(repo)}/pulls?state=open&per_page=100&page={pagina}", consumidor="revisar_codex")
        pulls += lote
        if len(lote) < 100:
            break
    return [{"number": p["number"], "headRefOid": p["head"]["sha"], "baseRefName": p["base"]["ref"],
             "headRefName": p["head"]["ref"], "isDraft": bool(p.get("draft"))} for p in pulls]


def revisar(repo, pr, sha, base_dados=None):
    diff = _gh("pr", "diff", str(pr), "-R", config.repo(repo))
    tam = len(diff.encode())
    parcial = f" — diff de {tam // 1000} kB cortado em 150 kB: revisão parcial" if tam > CORTE else ""
    corte = diff.encode()[:CORTE].decode(errors="ignore")
    texto = claude.perguntar(f"{PEDIDO}\n\n```diff\n{corte}\n```\n", modelo="opus", nome="revisar-codex",
                             sistema=SISTEMA, base_dados=base_dados, espera_trava=600).strip()
    # push durante a revisão: esta ficou velha; sem comentar nem marcar feito, o sha novo é revisado no próximo cron
    if json.loads(_gh("pr", "view", str(pr), "-R", config.repo(repo), "--json", "headRefOid"))["headRefOid"] != sha:
        return None
    url = _gh("pr", "comment", str(pr), "-R", config.repo(repo), "--body",
              f"**Revisão do Claude** (commit `{sha[:8]}`){parcial}\n\n{texto}").strip()
    return url, "Sem achados sérios" not in texto


def main(base_dados=None):
    pasta = conta.pasta(base_dados)
    pasta.mkdir(parents=True, exist_ok=True)
    with conta.trava(esperar=0, caminho=str(pasta / "revisar-codex.lock")) as pegou:
        if not pegou:
            return 0
        arq = pasta / "revisoes-claude.txt"
        feitos = set(arq.read_text(encoding="utf-8").split()) if arq.exists() else set()
        for repo in REPOS:
            try:
                prs = _abertos(repo)
            except github_api.CotaBaixa:
                print("GitHub com cota baixa: revisão do Codex pausada até o reset", file=sys.stderr)
                break
            except (subprocess.SubprocessError, OSError, ValueError, RuntimeError, KeyError) as e:
                print(f"{repo}: listar PRs falhou: {e}", file=sys.stderr)
                continue
            for p in prs:
                chave = f"{repo}#{p['number']}@{p['headRefOid']}"
                if p["isDraft"] or not p["headRefName"].startswith("codex/") or chave in feitos:
                    continue
                try:
                    feito = revisar(repo, p["number"], p["headRefOid"], base_dados)
                except (claude.ContaPausada, claude.ContaOcupada, RuntimeError, subprocess.SubprocessError, OSError,
                        ValueError, KeyError) as e:
                    print(f"{chave}: {e}", file=sys.stderr)  # sem feito: tenta de novo no próximo cron
                    continue
                if not feito:
                    print(f"{chave}: push durante a revisão, fica para o próximo cron", file=sys.stderr)
                    continue
                url, achados = feito
                with open(arq, "a", encoding="utf-8") as f:
                    f.write(chave + "\n")
                with open(pasta / "revisoes-claude.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps({"em": conta.iso(conta.agora()), "repo": repo, "pr": p["number"],
                                        "sha": p["headRefOid"][:8], "url": url, "achados": achados}) + "\n")
                feitos.add(chave)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
