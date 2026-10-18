// Abre uma página no Chrome headless (perfil novo, sem cookies) e diz o que ela carregou: as requisições, os cookies
// e se window.gtag / window.clarity existem. Sem dependência: fala com o Chrome pelo protocolo DevTools num pipe.
//
//   node navegador.mjs <url> [--cookie nome=valor] [--espera 8000]
//
// Saída (stdout): JSON {url, final, requisicoes: [url...], cookies: [nome...], globais: {gtag, clarity, dataLayer}}.
// Chrome: $CHROME_PATH ou o primeiro de google-chrome / chromium encontrado. Argumentos extras: $CHROME_ARGS.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const opcoes = {};
let url;
for (let i = 2; i < process.argv.length; i++) {
  const a = process.argv[i];
  if (a.startsWith("--")) opcoes[a.slice(2)] = process.argv[++i];
  else url = a;
}
const cookie = opcoes.cookie;
const espera = Number(opcoes.espera ?? 8000);
if (!url) {
  console.error("uso: node navegador.mjs <url> [--cookie nome=valor] [--espera ms]");
  process.exit(2);
}

function acharChrome() {
  const candidatos = [process.env.CHROME_PATH, "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium",
    "/usr/bin/chromium-browser", "/snap/bin/chromium", "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"];
  return candidatos.find((c) => c && existsSync(c));
}

const chrome = acharChrome();
if (!chrome) {
  console.error("Chrome/Chromium não encontrado (defina CHROME_PATH)");
  process.exit(3);
}

const perfil = mkdtempSync(join(tmpdir(), "verificador-chrome-"));
const flags = ["--headless=new", "--remote-debugging-pipe", `--user-data-dir=${perfil}`, "--no-first-run",
  "--no-default-browser-check", "--disable-gpu", "--disable-extensions", "--disable-dev-shm-usage", "--mute-audio",
  "--disable-background-networking", "--disable-sync", "--window-size=1366,900",
  ...(process.getuid?.() === 0 ? ["--no-sandbox"] : []),
  ...(process.env.CHROME_ARGS ? process.env.CHROME_ARGS.split(" ").filter(Boolean) : []), "about:blank"];
const proc = spawn(chrome, flags, { stdio: ["ignore", "ignore", "ignore", "pipe", "pipe"] });
const [, , , entrada, saida] = proc.stdio;

let proximo = 0;
const pendentes = new Map();
const ouvintes = [];
let buffer = Buffer.alloc(0);
saida.on("data", (pedaco) => {
  buffer = Buffer.concat([buffer, pedaco]);
  let fim;
  while ((fim = buffer.indexOf(0)) !== -1) {
    const msg = JSON.parse(buffer.subarray(0, fim).toString("utf8"));
    buffer = buffer.subarray(fim + 1);
    if (msg.id !== undefined && pendentes.has(msg.id)) {
      const { ok, erro } = pendentes.get(msg.id);
      pendentes.delete(msg.id);
      msg.error ? erro(new Error(`${msg.error.message} (${msg.error.code})`)) : ok(msg.result);
    } else if (msg.method) {
      for (const f of ouvintes) f(msg);
    }
  }
});

function cdp(method, params = {}, sessionId) {
  const id = ++proximo;
  entrada.write(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }) + "\0");
  return new Promise((ok, erro) => pendentes.set(id, { ok, erro }));
}

let encerrado = false;
function encerrar(codigo, texto) {
  if (encerrado) return;
  encerrado = true;
  if (texto) (codigo ? process.stderr : process.stdout).write(texto + "\n");
  try { proc.kill("SIGKILL"); } catch {}
  setTimeout(() => {
    try { rmSync(perfil, { recursive: true, force: true }); } catch {}
    process.exit(codigo);
  }, 300);
}
setTimeout(() => encerrar(4, "tempo esgotado"), espera + 40000);
proc.on("exit", (c) => encerrar(5, `Chrome saiu (código ${c})`));

try {
  const { targetId } = await cdp("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp("Target.attachToTarget", { targetId, flatten: true });
  const s = (m, p) => cdp(m, p, sessionId);
  const requisicoes = [];
  let carregou;
  const carregada = new Promise((ok) => (carregou = ok));
  ouvintes.push((msg) => {
    if (msg.sessionId !== sessionId) return;
    if (msg.method === "Network.requestWillBeSent") requisicoes.push(msg.params.request.url);
    if (msg.method === "Page.loadEventFired") carregou();
  });
  await s("Network.enable");
  await s("Page.enable");
  const { userAgent } = await cdp("Browser.getVersion");
  await s("Network.setUserAgentOverride", { userAgent: userAgent.replace("HeadlessChrome", "Chrome") });
  if (cookie) {
    const [nome, ...valor] = cookie.split("=");
    await s("Network.setCookie", { name: nome, value: valor.join("="), url, path: "/" });
  }
  await s("Page.navigate", { url });
  await Promise.race([carregada, new Promise((ok) => setTimeout(ok, 30000))]);
  // Scripts com lazyOnload / requestIdleCallback entram depois do load: espera e rola a página como gente.
  await s("Runtime.evaluate", { expression: "window.scrollTo(0, 400)" });
  await new Promise((ok) => setTimeout(ok, espera));
  const { result } = await s("Runtime.evaluate", {
    returnByValue: true,
    expression: `({final: location.href, gtag: typeof window.gtag === "function", clarity: typeof window.clarity === "function",
                  dataLayer: Array.isArray(window.dataLayer) ? window.dataLayer.length : null})`,
  });
  const { cookies } = await s("Network.getCookies", { urls: [result.value.final] });
  encerrar(0, JSON.stringify({
    url, final: result.value.final, requisicoes, cookies: cookies.map((c) => c.name).sort(),
    globais: { gtag: result.value.gtag, clarity: result.value.clarity, dataLayer: result.value.dataLayer },
  }));
} catch (e) {
  encerrar(6, `falhou: ${e.message}`);
}
