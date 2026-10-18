# Instalar o Krohnos

Do servidor limpo ao painel no ar em poucos minutos. O roteiro abaixo é o mesmo que `scripts/teste-instalacao.sh`
cronometra (meta: menos de 15 minutos, contando o build da imagem).

## Pré-requisitos

- Uma máquina Linux (VPS ou PC) com **Docker 24+ e o plugin Docker Compose v2** (`docker compose version`).
- 2 GB de RAM livres e uns 4 GB de disco (a imagem leva Python, Node 22, `git`, `gh` e o CLI do Claude Code).
- Um domínio (ou subdomínio) apontado para o servidor, se quiser abrir o painel pela internet com TLS.
- Opcional e depois: um token do GitHub e uma chave de API do Linear (o assistente pede; dá para pular).

Nenhum segredo entra na imagem. Eles vivem em volumes do Docker (`cofre`, `dados`, `home`).

## 5 passos

```bash
# 1. baixe o código
git clone https://github.com/BRK13/krohnos.git krohnos && cd krohnos

# 2. construa a imagem (uma só serve os 4 serviços)
docker compose build painel

# 3. configure: `krohnos init` cria o krohnos.toml (volume `config`), a chave mestra, o cofre e o SEU usuário de dono
docker compose run --rm --no-deps --entrypoint krohnos painel init

# 4. suba os 4 serviços (painel, agenda, webhook, mcp)
docker compose up -d

# 5. confira e entre
curl -fsS http://127.0.0.1:8787/saude      # deve responder: ok
```

Depois abra o painel (veja "Proxy TLS" abaixo), entre com o e-mail e a senha do dono e siga o assistente em `/comecar`
(GitHub, Linear, primeiro projeto, primeira conta do Claude). O cookie de sessão é `Secure`: o navegador só o guarda em
HTTPS (ou em `http://localhost`); por `http://IP-DO-SERVIDOR:8787/` o login não segura.

Notas sobre o passo 3:

- O `krohnos.toml` fica no volume `config` (gravável: o assistente `/comecar` o reescreve). A chave mestra e o cofre
  ficam no volume `cofre`; os usuários do painel (`painel/usuarios.db`, só o hash da senha), o projeto e os exemplos, no
  volume `dados`.
- Telas de configuração (ex.: Agentes) gravam em `krohnos.painel.toml`, na pasta de dados, e não no `krohnos.toml`: o arquivo principal pode ser do root e somente leitura. A sobreposição vale por cima dele, chave a chave (env > sobreposição > `krohnos.toml` > padrão). Apague-a para voltar ao que o `krohnos.toml` diz.
- `krohnos init` pergunta tudo (domínio, nome e e-mail do dono, senha, organização do GitHub, primeiro projeto).
  Sem perguntas: no comando do passo 3, troque o final `init` por `init --sim --dominio krohnos.minhaempresa-com.example --dono-email ana@exemplo.test --projeto-dominio meusite-com-br.example`
  (ou `--exemplo` para os projetos de demonstração). A senha nunca vai pela linha de comando: deixe o `init` gerar
  (aparece uma vez, guarde) ou use `--dono-senha-env NOME_DA_VARIAVEL`.
- Rodar o `init` de novo é seguro: toml, chave, cofre, usuários e projeto que já existem são mantidos.
- Para ver o painel cheio antes de configurar o seu: `docker compose run --rm --no-deps --entrypoint krohnos painel demo`
  (dados inventados e marcados; `demo --limpar` remove só eles; recusa uma pasta de dados que já tem dado real).
- Para mudar a porta do painel: `KROHNOS_PAINEL_PORTA=9000 docker compose up -d`.

## Primeiro acesso: token de configuração

Quem abrir `/comecar` primeiro **não** vira dono. Se a instalação ainda não tem dono (o `krohnos init` não rodou, ou rodou
sem criar usuário), o painel gera um **token de configuração de uso único** e o mostra só no terminal (`krohnos init`) e
no log do contêiner (`docker compose logs painel`). Sem o token, `/comecar` mostra a tela "Configuração pendente: use o
token exibido na instalação". Token errado é recusado (5 erros seguidos bloqueiam por 15 min); o certo cria o dono uma
vez e é apagado. Depois disso `/comecar` pede o login do dono e, quando o assistente termina, dá 404. Com o passo 3
(`krohnos init`) o dono já existe e o token nem chega a ser gerado.

## Proxy TLS (por sua conta)

O painel fala HTTP na porta 8787. Ponha um proxy com TLS na frente. O webhook do Linear (8790) e o MCP (4181) já
ficam só no `127.0.0.1` do servidor.

Caddy (2 linhas, certificado automático):

```
krohnos.minhaempresa-com.example {
    reverse_proxy 127.0.0.1:8787
}
```

Traefik (arquivo dinâmico; troque o domínio, o resolvedor de certificado e o endereço do servidor):

```yaml
http:
  routers:
    krohnos-painel:
      rule: Host(`krohnos.minhaempresa-com.example`)
      service: krohnos-painel
      entryPoints: [websecure]
      tls:
        certResolver: letsencrypt
  services:
    krohnos-painel:
      loadBalancer:
        servers:
          - url: http://127.0.0.1:8787
```

O painel só escuta no `127.0.0.1` do servidor (`compose.yml`). Se o proxy roda em contêiner, ele não alcança esse loopback:
rode o proxy no próprio servidor, ou troque a publicação do painel no `compose.yml` para o IP do gateway do Docker. O webhook
do Linear aponta do mesmo jeito para `127.0.0.1:8790`.

## Rotinas do sistema na tela Rotinas

A tela `/config/rotinas` lista as rotinas do sistema lendo o crontab que o `krohnos.toml` indicar. Aponte para o do repositório:

```toml
[rotinas]
cron = "/caminho/do/seu/rotinas.cron"   # o arquivo de crontab que você mantém (não vem no repositório)
```

Sem essa linha a tela só mostra as rotinas de plugin. Liga/desliga grava `rotinas.desligadas` em `<dados>/krohnos.painel.toml`
(modo 0644, legível por todos os usuários do hub). `deploy/rotina.sh` consulta essa lista antes de rodar (`python3 -m hub.rotinas
desligada <nome>`), então vale para o cron do root e para a agenda do contêiner; `FORCAR=1` no ambiente ignora. Tarefas do Claude
editadas ou criadas na tela ficam em `<dados>/tarefas/*.md` e valem por cima da pasta `tarefas/` do repositório.

O painel precisa gravar em `<dados>/tarefas` (tarefas editadas ou criadas na tela) e em `<dados>/rotinas/manual` (log e resultado do
"Rodar agora" das rotinas de plugin). Crie as duas pastas para o usuário do painel (`USUARIO_PAINEL` é o usuário que roda o painel; no contêiner isso já vem pronto, o comando é para instalação direta no servidor, onde `HUB_DATA` é a pasta de dados):

```bash
USUARIO_PAINEL=<usuario-do-painel>
HUB_DATA=/var/lib/krohnos
sudo install -d -o "$USUARIO_PAINEL" -g "$USUARIO_PAINEL" "$HUB_DATA/tarefas" "$HUB_DATA/rotinas/manual"
```

"Rodar agora" de rotina do sistema (linha do crontab) vai pelo executor do root da VPS, que não vem no contêiner (numa instalação só por contêiner o pedido não é executado): o painel grava um pedido (`rodar-rotina-agora`, em `<dados>/vps/pedidos`,
que também precisa ser gravável pelo painel) e o resultado aparece em Último resultado ao recarregar. Rotina que já estava rodando
aparece como "pulada", nunca como ok.

## Rodar uma rotina na mão

As rotinas agendadas rodam no serviço `agenda`, uma por horário do `krohnos.toml` (`[rotinas] horarios`, em UTC).
Para rodar uma agora (a mesma trava e o mesmo registro de falha do horário normal):

```bash
docker compose exec agenda deploy/rotina.sh <nome> <comando> [argumentos...]
# exemplo: docker compose exec agenda deploy/rotina.sh verificar python3 verificar_site.py --todos
```

A saída aparece na tela e, em horário agendado, em `docker compose logs agenda`. Rotina que falha entra no resumo
diário.

## Socket do Docker: opcional e desligado

A `agenda` só faz ações seguras dentro dos volumes (rerodar rotina, limpar arquivos antigos). A ação
`reiniciar-servico` precisa falar com o Docker do servidor e por isso fica **desligada**: o compose não monta
`/var/run/docker.sock`. Montá-lo (e definir `KROHNOS_DOCKER_SOCKET=1` no serviço `agenda`) dá a quem controla a agenda
poder de root no servidor. Só ligue se entender isso; sem ele, reinicie na mão com `docker compose restart <serviço>`.

## Solução de problemas

| Sintoma | O que olhar |
|---|---|
| `/saude` não responde | `docker compose ps` e `docker compose logs painel`. A primeira subida leva uns 20 s. |
| Esqueci a senha do painel | Troque pelo servidor: `docker compose exec painel python3 -m hub.usuarios senha <email>` (pede a nova; derruba as sessões abertas). Sem usuário nenhum: `docker compose exec painel python3 -m hub.usuarios criar <email> --papel dono`. Fora do contêiner (VPS sem Docker), rode como o usuário do painel, nunca como root: `sudo -u "$USUARIO_PAINEL" HUB_DATA=/var/lib/krohnos python3 -m hub.usuarios ...`. |
| Atualizei a VPS antiga e não sei o login | Na VPS antiga (sem krohnos.toml) o dono migrado entra com o login `dono` e a mesma senha de antes; troque depois em `/config/usuarios`. |
| Login certo, mas o painel volta ao /entrar | O cookie é `Secure`: abra por HTTPS (proxy TLS) ou por `http://localhost`. |
| `/comecar` dá 404 | O assistente já terminou. É o esperado. |
| Porta já em uso | `KROHNOS_PAINEL_PORTA`, `KROHNOS_WEBHOOK_PORTA` e `KROHNOS_MCP_PORTA` mudam as portas do servidor. |
| `krohnos init` recusa: "cofre existe mas a chave mestra não" | Recupere a chave mestra do backup. O `init` nunca cria outra por cima de um cofre existente. |
| Build lento ou sem rede | O build baixa pacotes do Debian, Node e npm. Repita; as camadas prontas ficam em cache. |
| Verificação de um site mostra itens "não verificado" | Itens de Search Console e Bing precisam das credenciais do cofre; sem elas ficam assim de propósito. |
| Quero começar do zero | `docker compose down -v` apaga os volumes (dados, cofre e logins). Sem `-v`, só para os serviços. |

Para provar a instalação de ponta a ponta num servidor limpo: `scripts/teste-instalacao.sh --dind` (sobe um
`docker:dind` privilegiado como se fosse a VPS) ou `scripts/teste-instalacao.sh --local` (usa o docker da máquina,
com um projeto `krohnos-teste-<pid>` que ele mesmo remove no fim). Ambos imprimem o tempo de cada etapa e falham
acima de 900 s.
