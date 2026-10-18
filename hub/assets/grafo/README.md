# Grafo do Krohnos

`force-graph-1.49.4.min.js` é o bundle UMD oficial do pacote npm
`force-graph@1.49.4`, distribuído sob MIT (arquivo LICENSE ao lado).
Origem: https://registry.npmjs.org/force-graph/-/force-graph-1.49.4.tgz,
`package/dist/force-graph.min.js`. Inclui o renderizador canvas, d3-force
(Barnes–Hut) e interação de zoom/pan/toque, sem solicitações a CDN.

`grafo.js` é a integração do hub. A API `/mapa/grafo.json` é autenticada,
projeta apenas nós (`id`, `tipo`, `nome`, `grau`, `projeto`) e arestas (`de`, `para`, `tipo`).
O JSON fica em cache no processo por identidade do índice: quando o SQLite
muda, `hub.grafo._indice` entrega outro índice e o JSON é reconstruído.
O navegador continua usando `no-store`, como o restante do painel.


`/mapa` abre somente a arquitetura com física de força. Cada projeto
atrai seu grupo para um centro em torno das VPS neutras. Não há posições fixas,
caixas nem colunas. Cor representa projeto e anel distingue tipo. Nós sem projeto
ficam escondidos; a legenda permite mostrá-los em cinza escuro.
Somente projetos e VPS têm rótulos permanentes; outros exigem hover ou zoom >2.
Uma lista de retângulos por frame impede sobreposição. Arestas ficam a 15% de
opacidade e acendem no hover. Comandos/caminhos não ambíguos atribuem crons e
serviços; infraestrutura compartilhada não transmite projeto.

`/mapa/saude` é somente leitura: conta vizinhos distintos, lista evidências e
sugestões, e informa cobertura. SHA-256 de dotenv literal é calculado na coleta
local privilegiada; interpolação, multilinha e fontes remotas sem hash não são
comparadas. Nenhum hash ou valor entra no JSON visual. Cópias de memória são
comparadas por hash do texto sanitizado; divergência pede revisão humana,
sem afirmar contradição automaticamente. Os únicos reparos automáticos são
relações derivadas de nomes/aliases não ambíguos, refeitas na remontagem.
