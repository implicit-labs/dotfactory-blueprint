FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends python3 git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG CODEX_VERSION=0.154.0
ARG CLAUDE_VERSION=2.1.274
RUN npm install --global "@openai/codex@${CODEX_VERSION}" "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
    && npm cache clean --force
RUN groupadd --gid 10001 factory \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /opt/factory-home --shell /bin/bash factory \
    && mkdir -p /opt/factory-home/.ssh /data \
    && chown factory:factory /opt/factory-home/.ssh /data \
    && chmod 700 /opt/factory-home/.ssh /data
WORKDIR /opt/dotfactory
COPY factory/src/dotfactory/worker.py /opt/dotfactory/worker.py
COPY factory/src /opt/dotfactory/factory/src
COPY factory/workflows /opt/dotfactory/factory/workflows
COPY factory/factory.example.json /opt/dotfactory/factory/factory.example.json
COPY factory/examples /opt/dotfactory/factory/examples
COPY factory/deploy/owned-worker/start.py /opt/dotfactory/start.py
ENV HOME=/opt/factory-home CODEX_HOME=/data/codex CLAUDE_CONFIG_DIR=/data/claude \
    PYTHONPATH=/opt/dotfactory/factory/src \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
CMD ["python3", "/opt/dotfactory/start.py"]
