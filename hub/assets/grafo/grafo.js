/* Krohnos · KRN-948. force-graph 1.49.4 (MIT), bundled locally. */
(async () => {
  const host = document.getElementById('mapa-canvas');
  if (!host) return;
  const status = document.getElementById('mapa-status');
  const colors = {projeto:'#B0976A',repo:'#8EA382',dominio:'#C4AD82',app:'#C98E4E',vps:'#D07F6E',banco:'#D07F6E',cron:'#8A8378',servico:'#8A8378',segredo:'#A88BA0',pessoa:'#F5F1E7',decisao:'#D9D2C3',issue:'#BDB6A8',pr:'#BDB6A8',receita:'#8EA382',pagina:'#E3C98F',documento:'#8F897D',memoria:'#A88BA0',relatorio:'#8F897D',tarefa:'#8A8378'};
  try {
    document.getElementById('mapa-tipos').open = matchMedia('(min-width: 601px)').matches;
    const response = await fetch('/mapa/grafo.json');
    if (!response.ok) throw Error('HTTP');
    const data = await response.json();
    const nodes = data.nos, byId = new Map(nodes.map(n => [n.id,n]));
    const architecture = new Set(['projeto','pagina','repo','app','dominio','vps','banco','servico','cron','segredo']);
    const palette = ['#72c5f4','#f2a65a','#a6d785','#dba4ed','#f08080','#68d5bf','#e9d675','#929dff'];
    const projects = nodes.filter(n=>n.tipo==='projeto').sort((a,b)=>a.id.localeCompare(b.id));
    const projectColors = new Map(projects.map((n,i)=>[n.id,palette[i] || `hsl(${i*137.508%360} 65% 70%)`]));
    const color = n => n.tipo==='vps' ? '#b9bdb7' : projectColors.get(n.projeto) || '#484b47';
    projects.forEach(n=>{const item=document.createElement('button');item.type='button';item.textContent=n.nome;item.style.color=color(n);item.onclick=()=>graph.zoomToFit(500,40,x=>x.projeto===n.id);document.getElementById('mapa-projetos').append(item);});
    let showUnassigned=false;
    const unassigned=document.createElement('button');unassigned.type='button';unassigned.textContent='Mostrar sem projeto';unassigned.setAttribute('aria-pressed','false');unassigned.onclick=()=>{showUnassigned=!showUnassigned;unassigned.setAttribute('aria-pressed',String(showUnassigned));filter();};document.getElementById('mapa-legenda').append(unassigned);
    const links = data.arestas.map(a => ({source:a.de,target:a.para,tipo:a.tipo}));
    const id = n => typeof n === 'object' ? n.id : n;
    const neighbors = new Map(nodes.map(n => [n.id,new Set([n.id])]));
    links.forEach(l => {neighbors.get(l.source).add(l.target);neighbors.get(l.target).add(l.source);});
    const hidden = new Set(nodes.filter(n=>!architecture.has(n.tipo)).map(n=>n.tipo));
    let focus = null, initial = true;
    const active = n => !focus || neighbors.get(focus.id).has(n.id);
    const graph = ForceGraph()(host).backgroundColor('#171816').nodeId('id').nodeLabel(() => '')
      .nodeVal(n => n.tipo==='vps'?36: n.tipo==='projeto'?16:n.tipo==='pagina'?9:n.projeto?2:0.6).nodeColor(n => active(n) ? color(n) : '#34352f')
      .linkLabel(l=>l.tipo).linkLineDash(l=>l.tipo==='serve'?[7,4]:['usa','depende_de','menciona'].includes(l.tipo)?[2,4]:null)
      .linkWidth(.45).linkColor(l => focus && (id(l.source)===focus.id || id(l.target)===focus.id) ? '#d9d2c3b0' : '#a5ada326')
      .nodeCanvasObjectMode(()=> 'after')
      .nodeCanvasObject((n,ctx,scale)=>{
        if(!n.projeto && n.tipo!=='vps')return;
        const radius=n.tipo==='vps'?12:n.tipo==='projeto'?8:n.tipo==='pagina'?6:4;
        ctx.strokeStyle=color(n);ctx.lineWidth=1/scale;
        const typeIndex=Object.keys(colors).indexOf(n.tipo);
        ctx.setLineDash([Math.max(1,typeIndex+1)/scale,2/scale]);
        ctx.beginPath();ctx.arc(n.x,n.y,radius+2,0,2*Math.PI);ctx.stroke();ctx.setLineDash([]);
      })
      .onNodeHover(n => {
        focus = n;
        paint();
        host.style.cursor = n ? 'pointer' : 'grab';
        if (n) status.textContent = `${n.nome} · ${n.tipo} · ${n.grau} relações`;
      })
      .onNodeClick(n => {location.href = '/mapa/' + n.id.split('/').map(encodeURIComponent).join('/');})
      .onNodeDragEnd(n => {n.fx=undefined;n.fy=undefined;graph.d3ReheatSimulation();})
      .cooldownTicks(180).onEngineStop(() => {if (initial) {initial=false;graph.zoomToFit(700,35,undefined);}});
    const paint = () => graph.nodeColor(n => active(n) ? color(n) : '#34352f')
      .linkColor(l => focus && (id(l.source) === focus.id || id(l.target) === focus.id) ? '#d9d2c3b0' : '#a5ada326');
    // A force, not fixed coordinates: every node remains draggable and alive.
    const centers = new Map(projects.map((n,i)=>{
      const angle=2*Math.PI*i/projects.length-Math.PI/2;
      return [n.id,{x:Math.cos(angle)*650,y:Math.sin(angle)*650}];
    }));
    nodes.forEach((n,i)=>{const c=centers.get(n.projeto)||{x:0,y:0};n.x=c.x+Math.cos(i*2.4)*60;n.y=c.y+Math.sin(i*2.4)*60;});
    const vpsNodes=nodes.filter(n=>n.tipo==='vps');
    const vpsCenters=new Map(vpsNodes.map((n,i)=>[n.id,{x:Math.cos(i*2*Math.PI/vpsNodes.length)*100,y:Math.sin(i*2*Math.PI/vpsNodes.length)*100}]));
    let simulated=[], labelNodes=[];
    const cluster = alpha => {
      simulated.forEach(n=>{
        const c=n.tipo==='vps'?vpsCenters.get(n.id):centers.get(n.projeto)||{x:0,y:0};
        const strength=n.tipo==='projeto'?.16:n.tipo==='vps'?.12:.055;
        n.vx+=(c.x-n.x)*strength*alpha;n.vy+=(c.y-n.y)*strength*alpha;
      });
    };
    cluster.initialize=ns=>{simulated=ns;};
    graph.d3Force('center',null).d3Force('project',cluster);
    graph.d3Force('charge').strength(n=>n.tipo==='vps'?-300:n.tipo==='projeto'?-200:-25);
    graph.d3Force('link').distance(l=>l.source.tipo==='vps'||l.target.tipo==='vps'?450:45)
      .strength(l=>l.source.projeto===l.target.projeto?.18:.008);
    // Render labels once per frame, with priority and a shared collision index.
    graph.onRenderFramePost((ctx,scale)=>{
      const occupied=[];
      const ordered=focus?[focus,...labelNodes.filter(n=>n!==focus)]:labelNodes;
      ordered.forEach(n=>{
        const anchor=n.tipo==='projeto'||n.tipo==='vps';
        if(!anchor && n!==focus && scale<=(n.tipo==='pagina'?1:2))return;
        const point=graph.graph2ScreenCoords(n.x,n.y);
        if(point.x<0||point.y<0||point.x>graph.width()||point.y>graph.height())return;
        const font=anchor?12:11;
        ctx.font=`${font/scale}px sans-serif`;ctx.textAlign='center';ctx.textBaseline='middle';
        const w=(ctx.measureText(n.nome).width*scale+10),h=font+8;
        let box;
        // Anchor labels may move to an empty slot; other labels simply disappear.
        for(let attempt=0;attempt<(anchor?100:1);attempt++){
          const offset=18+Math.floor(attempt/2)*h;
          const y=point.y+(attempt%2?-offset:offset);
          const candidate={x:point.x-w/2,y:y-h/2,w,h};
          if(!occupied.some(r=>candidate.x<r.x+r.w&&candidate.x+w>r.x&&candidate.y<r.y+r.h&&candidate.y+h>r.y)) {box=candidate;break;}
        }
        if(!box)return;
        occupied.push(box);
        const pos=graph.screen2GraphCoords(box.x+box.w/2,box.y+box.h/2);
        ctx.fillStyle='#171816e8';ctx.fillRect(pos.x-w/2/scale,pos.y-h/2/scale,w/scale,h/scale);
        ctx.fillStyle=anchor?color(n):'#e4e6df';ctx.fillText(n.nome,pos.x,pos.y);
      });
    });
    const resize = () => graph.width(host.clientWidth).height(host.clientHeight);
    new ResizeObserver(resize).observe(host);resize();
    const filter = () => {
      focus=null;
      const visible=n=>!hidden.has(n.tipo)&&(n.projeto||n.tipo==='vps'||showUnassigned);
      graph.graphData({nodes:nodes.filter(visible),links:links.filter(l=>visible(byId.get(id(l.source)))&&visible(byId.get(id(l.target))))});
      const rank=n=>n.tipo==='projeto'?0:n.tipo==='vps'?1:2;
      labelNodes=[...graph.graphData().nodes].sort((a,b)=>rank(a)-rank(b)||a.id.localeCompare(b.id));
      initial=true;graph.d3ReheatSimulation();
      requestAnimationFrame(()=>graph.zoomToFit(0,35));
      status.textContent = `${graph.graphData().nodes.length} de ${nodes.length} entidades · ${graph.graphData().links.length} relações`;
    };
    const buttons = new Map();
    [...new Set(nodes.map(n => n.tipo))].sort().forEach(tipo => {
      const button = document.createElement('button');button.type='button';button.setAttribute('aria-pressed',String(!hidden.has(tipo)));
      const dot=document.createElement('i');dot.style.backgroundColor='#aaa99f';dot.style.border=`${1+Object.keys(colors).indexOf(tipo)%3}px dashed currentColor`;
      button.append(dot,document.createTextNode(tipo));buttons.set(tipo,button);
      button.onclick=()=>{hidden.has(tipo)?hidden.delete(tipo):hidden.add(tipo);button.setAttribute('aria-pressed',String(!hidden.has(tipo)));filter();};
      document.getElementById('mapa-legenda').append(button);
    });
    const input=document.getElementById('mapa-termo'), options=document.getElementById('mapa-opcoes');
    const normalize = s => s.normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLocaleLowerCase();
    input.oninput=()=>{
      options.replaceChildren();
      if (!input.value.trim()) return;
      nodes.filter(n=>normalize(n.nome+' '+n.id).includes(normalize(input.value))).slice(0,15).forEach(n=>{
        const option=document.createElement('option');option.value=n.id;option.label=n.nome;options.append(option);
      });
    };
    document.getElementById('mapa-busca').onsubmit=event=>{
      event.preventDefault();const q=normalize(input.value.trim());if(!q)return;
      const n=nodes.find(n=>normalize(n.id)===q || normalize(n.nome)===q) || nodes.find(n=>normalize(n.nome+' '+n.id).includes(q));
      if(!n){status.textContent='Nenhuma entidade encontrada.';return;}
      if(!n.projeto&&n.tipo!=='vps'){showUnassigned=true;unassigned.setAttribute('aria-pressed','true');filter();}
      if(hidden.delete(n.tipo)){buttons.get(n.tipo).setAttribute('aria-pressed','true');filter();}
      initial=false;focus=n;paint();graph.centerAt(n.x || 0,n.y || 0,700);graph.zoom(3,700);
      status.textContent=`${n.nome} · ${n.tipo} · ${n.grau} relações`;
      const ficha=document.getElementById('mapa-ficha');ficha.hidden=false;ficha.textContent=`Abrir ${n.nome} →`;ficha.href='/mapa/'+n.id.split('/').map(encodeURIComponent).join('/');
    };
    document.getElementById('mapa-ajustar').onclick=()=>{focus=null;paint();graph.zoomToFit(700,35);};
    if(matchMedia('(prefers-reduced-motion: reduce)').matches)graph.cooldownTicks(40);
    filter();
    if (!nodes.length) status.textContent='O grafo ainda não foi montado.';
  } catch (_) {status.textContent='Não foi possível carregar o grafo. Recarregue para tentar novamente.';}
})();
