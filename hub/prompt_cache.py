"""Cache explícito Claude/OpenRouter; prefixo estável antes da pergunta variável.

Contrato: https://openrouter.ai/docs/guides/best-practices/prompt-caching
OpenRouter inclui leitura e escrita em prompt_tokens; o uso interno separa as três parcelas.
"""
import logging

from hub import conta

agora = conta.agora


def claude(modelo):
    return modelo.lower().startswith("anthropic/claude")


def configurar(payload):
    if not claude(payload["model"]):
        return
    for msg in reversed(payload["messages"]):
        if msg["role"] == "system":
            msg["content"] = [{"type": "text", "text": msg["content"],
                               "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
            return


def registrar(modelo, rota, resposta, inicio):
    if not claude(modelo):
        return
    u = resposta.get("usage") or {}
    detalhes = u.get("prompt_tokens_details") or {}
    leitura = detalhes.get("cached_tokens") or 0
    escrita = detalhes.get("cache_write_tokens") or 0
    entrada = max(0, (u.get("prompt_tokens") or 0) - leitura - escrita)
    conta.registrar({"inicio": conta.iso(inicio), "fim": conta.iso(agora()), "origem": "openrouter",
                     "nome": rota, "modelo": modelo, "ok": not resposta.get("error"),
                     "custo_usd": u.get("cost"),
                     "cache_read_input_tokens": leitura, "cache_creation_input_tokens": escrita,
                     "tokens": {"entrada": entrada, "saida": u.get("completion_tokens") or 0,
                                "cache_leitura": leitura, "cache_escrita": escrita}})
    logging.getLogger(__name__).info("%s cache_read_input_tokens=%s cache_creation_input_tokens=%s",
                                   rota, leitura, escrita)
