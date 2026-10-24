// Patch com guarda de versão/formato; aplicado a cada deploy, inclusive sem npm ci.
import fs from 'node:fs';
import path from 'node:path';
const dest = process.argv[2];
const pkg = JSON.parse(fs.readFileSync(path.join(dest, 'package.json')));
if (pkg.version !== '6.0.0') throw new Error('Reavaliar patch AI Bot Access para a nova versão do SEOmator');
const source = fs.readFileSync(new URL('./ai-bot-access.mjs', import.meta.url), 'utf8').replace(/^[\s\S]*?export function/, 'function');
const upstream = fs.readFileSync(new URL('./ai-bot-access-original.txt', import.meta.url), 'utf8');
const pending = [];
const dist = path.join(dest, 'dist');
let count = 0;
for (const file of fs.readdirSync(dist).filter(f => f.endsWith('.js'))) {
  const name = path.join(dist, file);
  const original = fs.readFileSync(name, 'utf8');
  if (!original.includes('function findBlockedAiBots(')) continue;
  const current = original.includes(source) ? source : upstream;
  if (original.split(current).length !== 2) throw new Error('Corpo inesperado: ' + file);
  const call = original.includes('findBlockedAiBots(robotsContent, context.url)')
    ? 'findBlockedAiBots(robotsContent, context.url)' : 'findBlockedAiBots(robotsContent)';
  if (original.split(call).length !== 2) throw new Error('Chamada inesperada: ' + file);
  let patched = original.replace(current, () => source)
    .replace(call, 'findBlockedAiBots(robotsContent, context.url)');
  // KRN-1522: exceção de política somente no ClienteJ. Google-Extended
  // também controla grounding; o bloqueio aceito não deve virar alerta acionável.
  const before = 'const blockedCitationBots = blockedBots.filter((bot) => CITATION_BOTS.includes(bot));';
  const after = `const policyBlockedBots = new URL(context.url).hostname === "clientej-com-br.example"
      ? ["ClaudeBot", "Google-Extended"] : [];
    const citationBotsForSite = CITATION_BOTS.filter(bot => !policyBlockedBots.includes(bot));
    const blockedCitationBots = blockedBots.filter((bot) => citationBotsForSite.includes(bot));`;
  if (!patched.includes(after)) {
    if (patched.split(before).length !== 2) throw new Error('Classificação inesperada: ' + file);
    patched = patched.replace(before, after)
      .replace('blockedCitationBots.length >= CITATION_BOTS.length', 'blockedCitationBots.length >= citationBotsForSite.length')
      .replace('All ${CITATION_BOTS.length} AI answer engines', 'All ${citationBotsForSite.length} AI answer engines');
  }
  if (!patched.includes('policyBlockedBots: blockedBots.filter')) patched = patched.replace('      blockedCitationBots,\n', '      blockedCitationBots,\n      policyBlockedBots: blockedBots.filter(bot => policyBlockedBots.includes(bot)),\n')
    .replace('Answer-engine crawlers are allowed; ${blockedTrainingBots2.length} training-only crawler(s) blocked:', 'Answer-engine crawlers required by site policy are allowed; ${blockedTrainingBots2.length} training crawler(s) blocked:')
    .replace('Blocking training-only crawlers does not affect visibility in AI answers.', 'Policy exceptions are reported in policyBlockedBots (KRN-1522); Google-Extended also controls Gemini grounding.');
  if (!patched.includes(source)) throw new Error('Função não substituída: ' + file);
  pending.push([name, patched]);
  count++;
}
if (count !== 2) throw new Error('Esperados dois bundles SEOmator, encontrados ' + count);
for (const [name, patched] of pending) fs.writeFileSync(name, patched);
console.log('AI Bot Access corrigido nos dois bundles (CLI e biblioteca)');
