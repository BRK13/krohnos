// node --test deploy/seomator/dobra.test.mjs   (funções puras; a medição no navegador é provada no lote.mjs instalado)
import test from 'node:test';
import assert from 'node:assert/strict';
import { lcpEhMarcada, lcpHintsProcede, lazyProcede } from './dobra.mjs';

const foto = '/_next/image?url=%2Fclientei-sobre.webp&w=640&q=75';
const img = (top) => ({ chaves: [foto], lazy: true, top, altura: 400 });
const detalhes = { candidate: { type: 'image', src: foto }, aboveFoldImages: [{ src: foto, isLazy: true }] };

test('LCP em texto ou poster não é a imagem marcada', () => {
  assert.equal(lcpEhMarcada({ ehImg: false, fontes: [] }, [foto]), false);
  assert.equal(lcpHintsProcede({ imagens: [], lcp: { ehImg: false, fontes: [] } }, detalhes), false);
});
test('LCP em outra imagem não é a marcada', () => {
  assert.equal(lcpEhMarcada({ ehImg: true, fontes: ['/apresentacao-poster.webp'] }, [foto]), false);
});
test('LCP na imagem marcada procede, com chave igual por URL completa', () => {
  const lcp = { ehImg: true, fontes: [`https://x.com${foto}`] };
  assert.equal(lcpHintsProcede({ imagens: [], lcp }, detalhes), true);
  assert.equal(lazyProcede({ imagens: [img(100)], lcp }, detalhes), true);
});
test('lazy: LCP na marcada mas fora da dobra não procede', () => {
  const lcp = { ehImg: true, fontes: [foto] };
  assert.equal(lazyProcede({ imagens: [img(1200)], lcp }, detalhes), false);
});
test('lazy: LCP em texto não descarta quando a imagem lazy está na dobra', () => {
  assert.equal(lazyProcede({ imagens: [img(100)], lcp: { ehImg: false, fontes: [] } }, detalhes), true);
});
test('lazy: LCP em texto e imagem abaixo da dobra não procede', () => {
  assert.equal(lazyProcede({ imagens: [img(1200)], lcp: { ehImg: false, fontes: [] } }, detalhes), false);
});
test('sem medição, o achado fica', () => {
  assert.equal(lcpHintsProcede(null, detalhes), true);
  assert.equal(lazyProcede(null, detalhes), true);
  assert.equal(lcpHintsProcede({ imagens: [], lcp: null }, detalhes), true);
});
test('candidata que não é imagem (poster) não é julgada', () => {
  assert.equal(lcpHintsProcede({ imagens: [], lcp: { ehImg: false, fontes: [] } }, { candidate: { type: 'video-poster', src: 'p.webp' } }), true);
});
