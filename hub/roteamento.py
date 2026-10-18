"""Roteamento de modelo por tipo de trabalho: hub.json → "roteamento" é a única fonte da regra.
Regra = todas as condições presentes valem (labels, prioridade, label_prefixo, titulo); a primeira que casa decide;
nenhuma casa → "padrao". Regra sem condição nunca casa."""
import json
import re

from hub import config as config_hub


def config():
    try:  # mesma leitura de agenda._hub_json (sem importar agenda: import circular)
        cfg = config_hub.ler_hub_json().get("roteamento")
    except (OSError, ValueError, AttributeError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _casa(r, labels, titulo, prioridade):
    conds = []
    if "labels" in r:
        conds.append(bool(set(labels) & {x.lower() for x in r["labels"]}))
    if "prioridade" in r:
        conds.append(prioridade == r["prioridade"])
    if "label_prefixo" in r:
        conds.append(any(lb.startswith(r["label_prefixo"]) for lb in labels))
    if "titulo" in r:
        try:
            conds.append(bool(re.search(r["titulo"], titulo or "", re.I)))
        except (re.error, TypeError):  # regex torto no hub.json: a regra não casa, as outras seguem
            return False
    return bool(conds) and all(conds)


def modelo(labels, titulo, prioridade, cfg=None):
    """(modelo, por): `labels` em minúsculas."""
    cfg = config() if cfg is None else cfg
    labels = {lb.lower() for lb in labels}
    for r in cfg.get("regras") or []:
        if _casa(r, labels, titulo, prioridade):
            return r["modelo"], r.get("por") or "regra"
    return cfg.get("padrao") or "sonnet", "padrão"


def tabela(cfg=None):
    """[(modelo, por)] na ordem das regras, fechando com o padrão; é o que o painel mostra."""
    cfg = config() if cfg is None else cfg
    return [(r["modelo"], r.get("por") or "regra") for r in cfg.get("regras") or []] + [(cfg.get("padrao") or "sonnet", "resto")]
