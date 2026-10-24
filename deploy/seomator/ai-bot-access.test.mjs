import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('./ai-bot-access.mjs', import.meta.url), 'utf8').replace('export function', 'function');
const check = (robots, path, bot = 'TestBot') => vm.runInNewContext(source + '\nfindBlockedAiBots(content, url)', { content: robots, url: 'https://example.com' + path, AI_BOTS: [bot], URL, Buffer }).length;
test('exceções públicas prevalecem sem abrir rotas privadas', () => {
  const r = 'User-agent: *\nAllow: /eleicoes-2026\nAllow: /partes/\nDisallow: /';
  assert.equal(check(r, '/eleicoes-2026/ac'), 0);
  assert.equal(check(r, '/partes/pessoa'), 0);
  assert.equal(check(r, '/privado'), 1);
});
test('grupo específico substitui curinga, mesmo sem Disallow', () => {
  assert.equal(check('User-agent: *\nDisallow: /\nUser-agent: TestBot\nAllow: /', '/'), 0);
});
test('une grupos específicos; comentários e linhas vazias não encerram grupo', () => {
  assert.equal(check('User-agent: TestBot\n\n# comentário\nDisallow: /\nUser-agent: TestBot\nAllow: /publico', '/publico'), 0);
});
test('wildcards, final, query e empate favorável a Allow', () => {
  const r = 'User-agent: *\nDisallow: /\nAllow: /$\nAllow: /publico\nDisallow: /publico/*/fim$\nAllow: /igual\nDisallow: /igual';
  assert.equal(check(r, '/'), 0);
  assert.equal(check(r, '/?x=1'), 1);
  assert.equal(check(r, '/publico/a/fim'), 1);
  assert.equal(check(r, '/publico/a/fim/mais'), 0);
  assert.equal(check(r, '/igual'), 0);
});
test('case insensitive nos agentes e case sensitive nos caminhos; vazio não bloqueia', () => {
  assert.equal(check('User-Agent: testbot\nDisallow:\nAllow: /', '/'), 0);
  assert.equal(check('User-agent: *\nDisallow: /Privado', '/privado'), 0);
});

test('instalador preserva JavaScript válido e é idempotente nos dois bundles', async () => {
  const os = await import('node:os');
  const path = await import('node:path');
  const { execFileSync } = await import('node:child_process');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'krn-1160-'));
  try {
    fs.mkdirSync(path.join(dir, 'dist'));
    fs.writeFileSync(path.join(dir, 'package.json'), '{"version":"6.0.0"}');
    const fixture = fs.readFileSync(new URL('./ai-bot-access-original.txt', import.meta.url), 'utf8') + '\nconst helper = 42;\nvar aiBotAccessRule = { run: context => { const blockedBots = findBlockedAiBots(robotsContent); const blockedCitationBots = blockedBots.filter((bot) => CITATION_BOTS.includes(bot)); return blockedCitationBots; } };';
    for (const name of ['index.js', 'chunk.js']) fs.writeFileSync(path.join(dir, 'dist', name), fixture);
    const script = new URL('./corrigir-ai-bot.mjs', import.meta.url);
    execFileSync(process.execPath, [script.pathname, dir]);
    const first = fs.readFileSync(path.join(dir, 'dist/index.js'), 'utf8');
    execFileSync(process.execPath, [script.pathname, dir]);
    assert.equal(fs.readFileSync(path.join(dir, 'dist/index.js'), 'utf8'), first);
    assert.ok(first.includes('findBlockedAiBots(robotsContent, context.url)'));
    assert.equal(vm.runInNewContext(first + '\naiBotAccessRule.run({url: \"https://example.com/publico\"}).length', { AI_BOTS: ['TestBot'], CITATION_BOTS: ['TestBot'], robotsContent: 'User-agent: *\nDisallow: /\nAllow: /publico', URL, Buffer }), 0);
    const classify = (host, bots) => vm.runInNewContext(first + '\naiBotAccessRule.run({url: "https://' + host + '/partes/pessoa"})', {AI_BOTS: bots, CITATION_BOTS: bots, robotsContent: 'User-agent: *\nDisallow: /', URL, Buffer});
    assert.equal(classify('clientej-com-br.example', ['ClaudeBot', 'Google-Extended']).length, 0);
    assert.equal(classify('example.com', ['ClaudeBot', 'Google-Extended']).length, 2);
    assert.equal(classify('clientej-com-br.example', ['OAI-SearchBot', 'PerplexityBot', 'Claude-SearchBot']).length, 3);
    assert.ok(first.includes('const helper = 42;'));
    for (const name of ['index.js', 'chunk.js']) execFileSync(process.execPath, ['--check', path.join(dir, 'dist', name)]);
    fs.writeFileSync(path.join(dir, 'dist/chunk.js'), fixture.replace('return blocked;', 'return [];'));
    assert.throws(() => execFileSync(process.execPath, [script.pathname, dir], {stdio: 'pipe'}));
    assert.equal(fs.readFileSync(path.join(dir, 'dist/index.js'), 'utf8'), first);
  } finally { fs.rmSync(dir, { recursive: true }); }
});
