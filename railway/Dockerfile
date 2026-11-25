# Harvey on Railway: the dashboard, the heartbeat loop and a small reverse
# proxy in one container, with all state on one volume mounted at /data.
# Build context is the repository root (see railway.json).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# curl: Claude CLI installer. libatomic1: needed by Playwright's bundled Node. git: data backup. caddy: public HTTPS front for the loopback-only
# dashboard. tini: PID 1 that forwards signals and reaps children.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates caddy tini libatomic1 git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/harvey

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

RUN groupadd --gid 1000 harvey \
    && useradd --create-home --uid 1000 --gid 1000 harvey \
    && chown -R harvey:harvey /opt/harvey /ms-playwright

# Everything railway/Dockerfile.dockerignore lets through: the package
# (harvey/, and sellbird/ after the upstream rename), prompts, skills, the
# default config and railway/.
COPY --chown=harvey:harvey . .
RUN chmod +x railway/entrypoint.sh railway/run.sh

# Claude Code CLI, installed as the runtime user; fail the build if Harvey's
# own isolation check does not pass against it.
USER harvey
ENV PATH="/home/harvey/.local/bin:${PATH}"
RUN installer="$(mktemp)" \
    && curl -fsSL https://claude.ai/install.sh -o "$installer" \
    && bash "$installer" \
    && rm "$installer" \
    && python -c "import importlib, os; pkg = 'sellbird' if os.path.isdir('sellbird') else 'harvey'; print(importlib.import_module(pkg + '.claude_runtime').check_cli())"

# The entrypoint starts as root only to prepare the volume (Railway mounts it
# root-owned), then drops to the harvey user for everything else.
USER root
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/harvey/railway/entrypoint.sh"]
