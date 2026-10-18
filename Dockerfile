# Krohnos Hub: imagem única; o compose roda um serviço por contêiner (deploy/entrada.sh).
# Nenhum segredo entra no build: ele vem do volume do cofre em tempo de execução.
FROM python:3.12-slim

ARG NODE_MAJOR=22
# 1 instala o CLI do Codex (opcional; a faixa codex do hub só liga com ele)
ARG INSTALAR_CODEX=0

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && mkdir -p -m 755 /etc/apt/keyrings \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && npm install -g @anthropic-ai/claude-code \
 && if [ "$INSTALAR_CODEX" = "1" ]; then npm install -g @openai/codex; fi \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home --shell /bin/bash krohnos \
 && mkdir -p /var/lib/krohnos /etc/krohnos/cofre /opt/krohnos \
 && chown -R krohnos:krohnos /var/lib/krohnos /etc/krohnos /opt/krohnos

WORKDIR /opt/krohnos
COPY requirements.txt /opt/krohnos/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=krohnos:krohnos . /opt/krohnos
# comando `krohnos` (krohnos init ...) dentro do contêiner
RUN [ -e /usr/local/bin/krohnos ] || ln -s /opt/krohnos/deploy/krohnos /usr/local/bin/krohnos
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 KROHNOS_CONFIG=/etc/krohnos/krohnos.toml
USER krohnos
EXPOSE 8787 8790 4181

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request as u; from hub import config; u.urlopen('http://127.0.0.1:%s/saude' % config.get('painel.porta'), timeout=4)" || exit 1

ENTRYPOINT ["/opt/krohnos/deploy/entrada.sh"]
CMD ["painel"]
