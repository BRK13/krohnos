// Medição no navegador para as regras perf-lazy-above-fold (KRN-1570) e perf-lcp-hints (KRN-1576).
// A regra do SEOmator trata as 3 primeiras <img> do DOM como "acima da dobra" sem medir nada: foto de rodapé ou de
// seção lá embaixo vira falso positivo (KRN-1563). Aqui a página é aberta em 1440x900 e só vale o lazy de imagem
// cujo getBoundingClientRect().top fica dentro da altura da janela.
// perf-lcp-hints só procede se o LCP medido é a img marcada; perf-lazy-above-fold só olha a posição (lazy na dobra procede mesmo sem ser LCP).
// Ambas as regras escolhem a candidata pelo DOM (primeira <img>), não pelo LCP real (KRN-1576): a foto de Sobre, abaixo da dobra,
// era marcada como LCP enquanto o LCP medido era o poster do vídeo ou um texto. Aqui o LCP vem do PerformanceObserver.
import { chromium } from 'playwright';

export const JANELA = { width: 1440, height: 900 };

// Chave de uma imagem: a URL completa (com w e q do /_next/image: a mesma foto sai em tamanhos diferentes, como avatar
// eager no topo e a versão lazy lá embaixo, e só a URL inteira separa as duas), relativa ou absoluta.
export function chave(src) {
  try {
    const u = new URL(src, 'http://x');
    return u.pathname + u.search;
  } catch {
    return String(src);
  }
}

// Dado as imagens medidas no navegador [{chaves, top, altura}] e as que a regra marcou como lazy, diz se alguma está na dobra.
// Só <img loading=lazy> conta. Imagem marcada que não achamos no DOM renderizado conta como na dobra (na dúvida o achado fica).
export function algumaNaDobra(medidas, marcadas, altura = JANELA.height) {
  return marcadas.some((src) => {
    const k = chave(src);
    const achadas = medidas.filter((m) => m.lazy && m.chaves.includes(k));
    if (!achadas.length) return true;
    return achadas.some((m) => m.altura > 0 && m.top < altura);
  });
}

// Dado o LCP medido {ehImg, fontes} e as imagens que a regra marcou, diz se o LCP é uma delas.
// Só procede quando o LCP medido é a <img> marcada; LCP em texto, poster de vídeo ou outra imagem = falso positivo.
export function lcpEhMarcada(lcp, marcadas) {
  if (!lcp?.ehImg) return false;
  const chaves = lcp.fontes.map(chave);
  return marcadas.some((src) => chaves.includes(chave(src)));
}

export async function abrirNavegador() {
  return chromium.launch({ headless: true });
}

// Abre a página em 1440x900 e devolve { imagens: [{chaves, lazy, top, altura}], lcp: {ehImg, fontes} | null }.
// null = não deu para medir (a regra mantém o achado). lcp null com imagens = o navegador não reportou LCP.
export async function medirPagina(navegador, url, timeout = 30000) {
  let ctx;
  try {
    ctx = await navegador.newContext({ viewport: JANELA });
    const pagina = await ctx.newPage();
    await pagina.goto(url, { waitUntil: 'load', timeout });
    const { imagens, lcp } = await pagina.evaluate(async () => {
      const fontesDe = (el) => [el.getAttribute('src'), el.currentSrc, ...(el.getAttribute('srcset') ?? '').split(',').map((s) => s.trim().split(/\s+/)[0])].filter(Boolean);
      // LCP final = última entrada; buffered traz o que já passou, e a espera curta deixa o último candidato chegar
      const entrada = await new Promise((resolve) => {
        let ultima = null;
        try {
          new PerformanceObserver((l) => { ultima = l.getEntries().at(-1) ?? ultima; }).observe({ type: 'largest-contentful-paint', buffered: true });
        } catch {
          return resolve(null);
        }
        setTimeout(() => resolve(ultima), 1000);
      });
      const el = entrada?.element;
      return {
        imagens: [...document.querySelectorAll('img')].map((i) => {
          const r = i.getBoundingClientRect();
          return { fontes: fontesDe(i), lazy: i.loading === 'lazy', top: r.top, altura: r.height };
        }),
        lcp: entrada ? { ehImg: el?.tagName === 'IMG', fontes: el?.tagName === 'IMG' ? fontesDe(el) : [] } : null,
      };
    });
    for (const m of imagens) m.chaves = m.fontes.map(chave);
    return { imagens, lcp };
  } catch {
    return null;
  } finally {
    await ctx?.close().catch(() => {});
  }
}

// true = o achado procede (ou não deu para medir); false = falso positivo. `medida` vem de medirPagina.
export function lazyProcede(medida, detalhes) {
  const marcadas = (detalhes?.aboveFoldImages ?? []).filter((i) => i.isLazy).map((i) => i.src);
  if (!marcadas.length || !medida) return true;
  // Lazy na dobra prejudica o carregamento mesmo sem ser o LCP (revisão do Codex, PR 815): só a posição decide.
  return algumaNaDobra(medida.imagens, marcadas);
}

export function lcpHintsProcede(medida, detalhes) {
  const c = detalhes?.candidate;
  if (!c || c.type !== 'image' || !medida?.lcp) return true;
  return lcpEhMarcada(medida.lcp, [c.src]);
}
