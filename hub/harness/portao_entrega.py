"""Portão de entrega do Debugger (classe `fechou_sem_entrega`).

Em 21/10 o fechou a «tentativa 2/3» do INC-284 com "20 testes focados passaram… Sem mudança de código ou PR":
teste verde não é entrega, e a frase "sem mudança" sem causa nem prova deixa o incidente encerrado sem ninguém poder conferir.
Fechar uma tentativa ligada a um INC exige UMA destas, na própria nota:
- PR (URL ou `PR #N`) ou commit (`commit <sha>`), que `debugger.exigir_pr_fechado` confere no GitHub;
- laudo de "sem mudança de código" que cita o INC, traz `causa:` e `prova:` (KRN-N, INC-N, link, commit ou comando entre crases;
  "testes passaram" sozinho não é prova).

    from hub.harness import portao_entrega
    portao_entrega.exigir(nota, 'INC-284')      # ValueError com o que falta
"""
import re

RE_INC = re.compile(r'\bINC-\d+\b')
RE_PR = re.compile(r'github\.com/[\w.-]+/[\w.-]+/pull/\d+|\b(?:PRs?|pull\s+request)\s*#?\s*\d+', re.I)
RE_COMMIT = re.compile(r'\bcommits?\s+`?[0-9a-f]{7,40}\b', re.I)
RE_SEM_MUDANCA = re.compile(r'sem mudan[çc]a de c[óo]digo', re.I)
RE_CAMPO = r'(?:^|[\n;.(\-*]|\s)\s*\**{}\**\s*:\s*(?P<v>[^\n;]+)'
RE_PROVA_FORTE = re.compile(r'\b(?:KRN|INC)-\d+\b|https?://\S+|`[^`\n]{3,}`|\bcommit\s+[0-9a-f]{7,40}\b', re.I)
RE_SO_TESTE = re.compile(r'^\W*(?:\d+\s+)?(?:testes?|tests?)\b[^`]*$', re.I)


def _campo(nota, nome):
    m = re.search(RE_CAMPO.format(nome), nota, re.I)
    return m.group('v').strip(' .*') if m else ''


def faltas(nota, inc):
    """Lista do que falta para a nota fechar a tentativa do `inc`; vazia = passa."""
    nota = nota or ''
    if not RE_SEM_MUDANCA.search(nota):
        if RE_PR.search(nota) or RE_COMMIT.search(nota):
            return []
        return ['sem PR nem commit na nota: cite o PR/commit do conserto ou um laudo "sem mudança de código"']
    f = []
    if inc not in RE_INC.findall(nota):
        f.append(f'o laudo não cita {inc}')
    if len(_campo(nota, 'causa')) < 8:
        f.append('o laudo não traz `causa:`')
    prova = _campo(nota, 'prova')
    if not prova:
        f.append('o laudo não traz `prova:`')
    elif RE_SO_TESTE.match(prova) or not RE_PROVA_FORTE.search(prova):
        f.append('a `prova:` é só "testes passaram" (cite KRN-N, INC-N, link, commit ou o comando)')
    return f


def exigir(nota, inc):
    f = faltas(nota, inc)
    if f:
        raise ValueError(f'portão de entrega ({inc}): ' + '; '.join(f))
