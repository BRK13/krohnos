# Validação · 17/10/2026

- 16 testes focados do gerador, incluindo comandos iguais aos de docs/INSTALAR.md, nomes autorizados,
  ausência do sexto projeto, backlinks sem nofollow, CNPJ, copy institucional e determinismo.
- Navegador real em inglês e português, 320/390/768/1440 px: sem transbordamento, menu sem
  sobreposição, imagens carregadas, navegação, tema persistido e nenhuma exceção JavaScript.
- Leitura sem JavaScript e preferência por movimento reduzido verificadas.
- `inspect.mjs` (18/10/2026): 0 FAIL nas duas línguas em 1440/768/390 px. Duas regras foram dispensadas por decisão de marca e política, não por medição: `few-pictures` (identidade de ferramenta dev, sem fotografias na tela de entrada) e `no-footage` (sem vídeo gerado automático, BRU-825). Três WARN aceitos: combinação Inter, Instrument Serif itálico e monospace pedida na identidade, e movimento discreto (a fita de luz, `hero-gl`, é o único movimento contínuo).
- Capturas desktop, celular e menu aberto conferidas visualmente. Capturas dos projetos vieram
  das páginas públicas em 17/10/2026, com consentimento opcional de medição recusado quando disponível.
- Não executado build de produção ou suíte completa localmente. Geração temporária somente nos testes focados.
- Publicação e autodeploy ainda exigem revisão do PR, operação administrativa e prova de um merge no ar.
  O HTML final contém `meta[name=krohnos-release]` com `2026-10-17` para a conferência após o deploy.

## Comparação com a SERP real

Consulta via `get_serp_results` do hub em 17/10/2026: `self hosted multi agent orchestrator`,
Google Estados Unidos, inglês, três resultados orgânicos:

1. https://github.com/sachi097/multi-agent-orchestator — repositório técnico, lido com HTTP 200.
2. https://www.reddit.com/r/automation/comments/1sjz4cl/which_is_the_best_fullymanaged_multiagent/ — discussão; a leitura direta retornou 403.
3. https://managed-code.com/blog-post/local-first-ai-agent-orchestration — artigo técnico, lido com HTTP 200.

Para o leitor que avalia uma operação empresarial concreta, a página oferece evidência local datada,
cinco projetos verificáveis e critérios de conclusão, além da descrição técnica. É essa a vantagem
editorial pretendida; não afirma superioridade técnica sobre concorrentes, cuja documentação pode ser
mais apropriada para implementação. Não foi possível avaliar integralmente a discussão bloqueada.
A busca equivalente em português trouxe uma discussão sobre Claude Code e um curso de OpenClaw.
