"""Instala a referência do hub nas skills do Fulano e apaga as skills openseo-* antigas."""
import re
import shutil
from pathlib import Path

root=Path(__file__).resolve().parent
home=Path.home()
reference=(root/'INTEGRATION.md').read_text(encoding='utf-8')
removidas=0
for agent in ('.codex','.claude'):
    skills=home/agent/'skills'
    for old in skills.glob('openseo-*'):  # o OpenSEO foi desligado; as skills dele saem
        shutil.rmtree(old,ignore_errors=True)
        removidas+=1
    dest=skills/'nimbus'
    (dest/'references/hub.md').write_text(reference,encoding='utf-8')
    entry=dest/'SKILL.md'
    body=entry.read_text(encoding='utf-8')
    marker='## Integrações do Hub'
    secao=marker+'\n\nSEO e analytics: MCP do hub (http://localhost:4181/mcp, por túnel SSH). Leia [integrações](references/hub.md).\n'
    body=re.sub(re.escape(marker)+r'.*?(?=\n## |\Z)',lambda _:secao.rstrip('\n'),body,flags=re.S) if marker in body else body+'\n\n'+secao
    entry.write_text(body,encoding='utf-8')
plugin=home/'.nimbus/plugin'
(plugin/'references/hub.md').write_text(reference,encoding='utf-8')
pc=plugin/'references/pc-local.md'
body=pc.read_text(encoding='utf-8')
if '## Integrações do Hub' not in body:body+='\n\n## Integrações do Hub\n\nLeia [hub.md](hub.md) para o MCP do hub, GEO.new, ranking e rotinas VPS.\n'
pc.write_text(body,encoding='utf-8')
for agent in ('.codex','.claude'):
    cache=home/agent/'plugins/cache/nimbus-local/nimbus/2.2.0-fulano.1/references'
    if cache.exists():
        shutil.copy2(plugin/'references/hub.md',cache/'hub.md')
        shutil.copy2(pc,cache/'pc-local.md')
# so.mjs: a cópia versionada é a do hub (nimbus/so.mjs). As outras (ferramenta do PC e as que as skills lp e
# blog levam junto para rodar na nuvem) são cópias dela, nunca editadas à mão.
so = root/'nimbus/so.mjs'
destinos = [home/'.nimbus/ferramentas/so.mjs', plugin/'skills/blog/so.mjs', plugin/'skills/lp/so.mjs']
for agent in ('.codex', '.claude'):
    for skill in ('blog', 'lp'):
        destinos.append(home/agent/'plugins/cache/nimbus-local/nimbus/2.2.0-fulano.1/skills'/skill/'so.mjs')
atualizados = 0
for dest in destinos:
    if dest.parent.exists() and (not dest.exists() or dest.read_bytes() != so.read_bytes()):
        shutil.copy2(so, dest)
        atualizados += 1
print(f'Referência do hub instalada no Codex e no Claude; {removidas} skills openseo-* removidas; so.mjs sincronizado ({atualizados} cópias atualizadas).')
