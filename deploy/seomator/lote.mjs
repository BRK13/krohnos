// Audita uma lista de URLs com o SEOmator, uma por vez, e grava um resumo compacto (KRN-809).
// Uso: node lote.mjs <entrada.json> <saida.json>      entrada: {"urls": [...], "timeout": 30000}
// Por que não `seomator audit --crawl`: o crawl guarda o HTML de todas as páginas na memória do node e passa de 4 GB
// em ~300 páginas (SIGABRT, saída -6). Aqui cada página é auditada e descartada: a memória não cresce com o tamanho do lote,
// e a lista de URLs vem do sitemap, então dá para fatiar um site grande em seções.
// Roda de /opt/seomator (copiado por deploy/seomator.sh) para achar o pacote; respeita SEOMATOR_HOME.
import fs from 'node:fs';
import * as m from '@seomator/seo-audit';
import { abrirNavegador, lazyProcede, lcpHintsProcede, medirPagina } from './dobra.mjs';

const [entrada, saida] = process.argv.slice(2);
const { urls, timeout = 30000 } = JSON.parse(fs.readFileSync(entrada, 'utf8'));

const auditor = m.createAuditor({ measureCwv: true, timeout });
// robots.txt e sitemap são do site, não da página: buscados uma vez por processo (por página seriam centenas de downloads).
let site;
auditor.enrichContext = async function (contexto, url) {
  if (!site) {
    const robots = await this.fetchRobotsTxt(url);
    site = { robots, sitemap: await this.fetchSitemap(url, robots) };
  }
  contexto.robotsTxtContent = site.robots;
  this.applySitemapData(contexto, site.sitemap);
};

const paginas = []; // uma entrada por página, só com fail/warn (pass não interessa e é o grosso do volume)
const erros = [];
let meta = {};
let navegador; // aberto só se alguma página tiver perf-lazy-above-fold (KRN-1570) ou perf-lcp-hints (KRN-1576); null = não subiu

for (const url of urls) {
  try {
    const r = await auditor.audit(url);
    const achados = [];
    let medida; // uma abertura da página serve às duas regras; undefined = ainda não medida
    const cats = {};
    for (const c of r.categoryResults ?? []) {
      cats[c.categoryId] = c.score ?? null;
      for (const x of c.results ?? []) {
        if (x.status !== 'fail' && x.status !== 'warn') continue;
        if (x.ruleId === 'perf-lazy-above-fold' || x.ruleId === 'perf-lcp-hints') {
          // as duas regras escolhem a imagem pelo DOM (KRN-1570, KRN-1576): só ficam se o LCP medido for a <img> marcada
          // (lazy-above-fold também exige a imagem na dobra de 1440x900). Chromium que não sobe ou medição que falha
          // não derrubam a auditoria: o achado fica e não se tenta de novo
          if (navegador === undefined) navegador = await abrirNavegador().catch(() => null);
          if (medida === undefined) medida = navegador ? await medirPagina(navegador, url, timeout) : null;
          const procede = x.ruleId === 'perf-lcp-hints' ? lcpHintsProcede : lazyProcede;
          if (!procede(medida, x.details)) continue;
        }
        achados.push({ ruleId: x.ruleId, cat: c.categoryId, status: x.status, weight: x.weight ?? 0, message: x.message ?? null });
      }
    }
    paginas.push({ url, nota: r.overallScore ?? null, cats, achados });
    if (!Object.keys(meta).length) {
      // nome e correção de cada regra vêm do audit salvo no banco (SEOMATOR_HOME temporário)
      const db = m.getAuditsDatabase();
      const salvo = m.saveAuditToDatabase(r);
      meta = m.getAuditDetail(db, salvo.auditId)?.ruleMetadata ?? {};
    }
  } catch (e) {
    // erro HTTP da página (404, 500) é achado do site; timeout e falha de render são do ambiente e não entram
    erros.push({ url, erro: String(e?.message ?? e).slice(0, 200), http: /HTTP (\d+)/.exec(String(e?.message))?.[1] ?? null });
  }
}
await navegador?.close();
m.closeAuditsDatabase();
fs.writeFileSync(saida, JSON.stringify({ paginas, erros, meta }));
