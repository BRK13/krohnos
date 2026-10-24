// Correção local do SEOmator 6.0.0: avaliar o caminho auditado e o grupo mais específico.
export function findBlockedAiBots(content, url) {
  const groups = [];
  let group;
  for (const raw of content.split('\n')) {
    const line = raw.split('#')[0].trim();
    const colon = line.indexOf(':');
    if (colon < 0) continue;
    const key = line.slice(0, colon).trim().toLowerCase();
    const value = line.slice(colon + 1).trim();
    if (key === 'user-agent') {
      if (!group || group.started) {
        group = { agents: [], rules: [], started: false };
        groups.push(group);
      }
      group.agents.push(value.toLowerCase());
    } else if (group) {
      group.started = true;
      if ((key === 'allow' || key === 'disallow') && value) group.rules.push({ value, allow: key === 'allow' });
    }
  }
  const target = new URL(url);
  const path = target.pathname + target.search;
  return AI_BOTS.filter(bot => {
    const name = bot.toLowerCase();
    const specific = groups.filter(g => g.agents.includes(name));
    const selected = specific.length ? specific : groups.filter(g => g.agents.includes('*'));
    let winner;
    for (const { rules } of selected) for (const rule of rules) {
      const anchored = rule.value.endsWith('$');
      const pattern = anchored ? rule.value.slice(0, -1) : rule.value;
      const expression = pattern.split('*').map(s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.*');
      if (!new RegExp('^' + expression + (anchored ? '$' : '')).test(path)) continue;
      const length = Buffer.byteLength(pattern.replaceAll('*', ''));
      if (!winner || length > winner.length || (length === winner.length && rule.allow)) winner = { length, allow: rule.allow };
    }
    return winner?.allow === false;
  });
}
