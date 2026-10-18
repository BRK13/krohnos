import fs from 'node:fs';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const root = path.dirname(fileURLToPath(import.meta.url));
const data = process.env.HUB_DATA || path.join(root, 'data');
// Cadastro único: <projetos>/<id>.json (validado por python -m hub.sites validar). Inativo (ativo:false) não entra: nem coleta, nem tabela, nem KPI.
// A pasta vem do mesmo resolvedor do Python (env > kronos.toml > legado > exemplos/); KRONOS_PROJETOS dispensa o Python.
const sitesDir = process.env.KRONOS_PROJETOS || execFileSync('python3',['-c','from hub import config; print(config.projetos())'],{cwd:root,encoding:'utf8'}).trim();
const cadastro = fs.readdirSync(sitesDir).filter(f=>f.endsWith('.json')).sort().map(f=>JSON.parse(fs.readFileSync(path.join(sitesDir,f),'utf8')));
if(!cadastro.length || cadastro.some(s=>!s.id||!s.dominio||!s.ids)) throw new Error(sitesDir+' vazio ou inválido: rode python -m hub.sites validar');
const sites = cadastro.filter(s=>s.ativo!==false);
// Clarity sem token de exportação no cofre é "falta token" (só o dono do projeto gera), não falha de coleta.
const credDir = path.join(process.env.NIMBUS_HOME || path.join(process.env.HOME||'', '.nimbus'), 'credenciais');
const today = new Date().toISOString().slice(0,10);
const out = path.join(data, 'snapshots', today);
fs.mkdirSync(out, {recursive:true});
const summary = {at:new Date().toISOString(),projects:[], note:'Snapshots de períodos móveis não devem ser somados. GA4 mede visitas; não comprova citações.'};
function run(args) {
  const raw = execFileSync(process.execPath,[path.join(root,'nimbus/so.mjs'),...args],{timeout:180000,encoding:'utf8',stdio:['ignore','pipe','pipe']});
  return JSON.parse(raw);
}
function write(name, value) { const target=path.join(out,name+'.json'); fs.writeFileSync(target+'.tmp',JSON.stringify(value,null,2)); fs.renameSync(target+'.tmp',target); }
for (const site of sites) {
  const project={id:site.id,domain:site.dominio,...site.ids};
  const status={id:project.id,domain:project.domain,ativo:site.ativo,checks:{}};
  const tasks = [['status',['site','status','--url','https://'+project.domain]]];
  if(project.gsc) tasks.push(['gsc',['google','gsc-consultas','--site',project.gsc,'--dias','7','--dimensoes','date','--limite','100']]);
  if(project.ga4) tasks.push(['ga4',['google','ga4-relatorio','--propriedade',project.ga4,'--dias','7','--dimensoes','sessionSource','--metricas','sessions,engagedSessions,keyEvents','--limite','1000']]);
  if(project.ga4) tasks.push(['ga4-dia',['google','ga4-relatorio','--propriedade',project.ga4,'--dias','8','--dimensoes','date','--metricas','sessions,keyEvents','--limite','20']]);
  // Eventos do site (sites/<id>.json: medicao.eventos) por dia, do GA4: o mesmo nome também vai ao Clarity, cuja API de exportação não lista evento nem tag.
  if(project.ga4 && site.medicao?.eventos?.length) tasks.push(['ga4-eventos',['google','ga4-relatorio','--propriedade',project.ga4,'--dias','8','--dimensoes','date,eventName','--metricas','eventCount,totalUsers','--eventos',site.medicao.eventos.join(','),'--limite','10000']]);
  if(project.clarity && !fs.existsSync(path.join(credDir,'clarity',project.id+'.json'))) status.checks.clarity={ok:false,faltaToken:true};
  else if(project.clarity) tasks.push(['clarity',['clarity','--cliente',project.id,'--dias','3']]);
  for(const [name,args] of tasks) {
    const target=path.join(out,project.id+'-'+name+'.json');
    try {
      // A repeat on the same UTC date reuses successful results and respects Clarity quota; a saved ok:false is retried.
      const salvo=fs.existsSync(target)?JSON.parse(fs.readFileSync(target)):null;
      const value=salvo&&salvo.ok!==false?salvo:run(args);
      write(project.id+'-'+name,value);
      status.checks[name]=name==='status'?{ok:value.ok,google:value.google?.ok,bing:value.bing?.ok,ga4:value.ga4?.ok}:{ok:true};
      if(name==='ga4') {
        const aiHosts=['chatgpt.com','chat.openai.com','perplexity.ai','gemini.google.com','claude.ai','copilot.microsoft.com'];
        status.aiReferrals=(value.rows||[]).filter(r=>aiHosts.some(h=>r.dimensionValues[0].value===h || r.dimensionValues[0].value.endsWith('.'+h))).map(r=>({source:r.dimensionValues[0].value,sessions:Number(r.metricValues[0].value),engagedSessions:Number(r.metricValues[1].value),keyEvents:Number(r.metricValues[2].value)}));
      }
    } catch(e) {status.checks[name]={ok:false,error:'Coleta falhou; verificar permissão, quota ou conectividade',exitCode:e.status??null};}
  }
  summary.projects.push(status);
}
fs.writeFileSync(path.join(data,'latest.json'),JSON.stringify(summary,null,2));
const lines=['# Nimbus — coleta '+today,'','Períodos móveis: GSC/GA4 7 dias; Clarity 72 horas. Não somar snapshots.','','| Projeto | Google | Bing | GA4 | Clarity |','|---|---|---|---|---|'];
for(const p of summary.projects){const s=p.checks.status||{};const label=v=>v===true?'OK':'sem acesso/dado';lines.push(`| ${p.id} | ${label(s.google)} | ${label(s.bing)} | ${label(s.ga4)} | ${p.checks.clarity?.faltaToken?'falta token':p.checks.clarity?label(p.checks.clarity.ok):'não configurado'} |`);}
for(const p of summary.projects){if(p.aiReferrals?.length)lines.push('',`${p.id}: referências de IA observadas: `+p.aiReferrals.map(r=>`${r.source} (${r.sessions} sessões)`).join(', '),'');}
fs.writeFileSync(path.join(data,'latest-report.md'),lines.join('\n'));
console.log(JSON.stringify({at:summary.at,projects:summary.projects.length,report:path.join(data,'latest-report.md')}));
if(summary.projects.some(p=>Object.entries(p.checks).some(([k,v])=>k!=='status'&&!v.ok&&!v.faltaToken)))process.exitCode=1;
