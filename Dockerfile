FROM python:3.12-slim-bookworm AS build

# C4ai version
ARG C4AI_VER=0.9.2
ENV C4AI_VERSION=$C4AI_VER
LABEL c4ai.version=$C4AI_VER

# Set build arguments
ARG APP_HOME=/app
ARG GITHUB_REPO=https://github.com/unclecode/crawl4ai.git
ARG GITHUB_BRANCH=main
ARG USE_LOCAL=true

ENV PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    DEBIAN_FRONTEND=noninteractive \
    REDIS_HOST=localhost \
    REDIS_PORT=6379

ARG PYTHON_VERSION=3.12
ARG INSTALL_TYPE=default
ARG ENABLE_GPU=false
ARG TARGETARCH

# Redis version — pinned to a CVE-patched release by default.
# Override with --build-arg REDIS_VERSION="" for latest, or
# --build-arg REDIS_VERSION="6:7.2.7-1rl1~bookworm1" for a specific version.
ARG REDIS_VERSION="6:7.2.7-1rl1~bookworm1"

LABEL maintainer="unclecode"
LABEL description="🔥🕷️ Crawl4AI: Open-source LLM Friendly Web Crawler & scraper"
LABEL version="1.0"

# Install curl and gnupg first (needed to add Redis repo)
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg \
    && rm -rf /var/lib/apt/lists/*

# Add official Redis repository for security-patched versions
RUN curl -fsSL https://packages.redis.io/gpg | gpg --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb bookworm main" \
    > /etc/apt/sources.list.d/redis.list

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    wget \
    gnupg \
    git \
    cmake \
    pkg-config \
    python3-dev \
    libjpeg-dev \
    redis-tools${REDIS_VERSION:+=$REDIS_VERSION} \
    redis-server${REDIS_VERSION:+=$REDIS_VERSION} \
    supervisor \
    && apt-get clean \ 
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# nodriver stealth worker browser stack: apt chromium + a virtual framebuffer.
# Headful Chromium on Xvfb is the default: CF managed "Just a moment"
# interstitials do not clear in plain headless on this stack, but resolve in
# ~10-30s with a real X display (supervisord runs Xvfb on :99, DISPLAY=:99).
# openbox/xdotool + baseline fonts follow the proven cloak-browser pattern
# (fontconfig + emoji/CJK font layers keep canvas-hash fingerprints sane).
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    xvfb \
    xdotool \
    openbox \
    fontconfig \
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-unifont \
    fonts-freefont-ttf \
    fonts-ipafont-gothic \
    fonts-wqy-zenhei \
    fonts-tlwg-loma-otf \
    fonts-urw-base35 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f >/dev/null 2>&1 || true

RUN apt-get update && apt-get dist-upgrade -y \
    && rm -rf /var/lib/apt/lists/*

RUN if [ "$ENABLE_GPU" = "true" ] && [ "$TARGETARCH" = "amd64" ] ; then \
    echo "deb http://deb.debian.org/debian bookworm contrib non-free" >> /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    nvidia-cuda-toolkit \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* ; \
else \
    echo "Skipping NVIDIA CUDA Toolkit installation (unsupported platform or GPU disabled)"; \
fi

RUN if [ "$TARGETARCH" = "arm64" ]; then \
    echo "🦾 Installing ARM-specific optimizations"; \
    apt-get update && apt-get install -y --no-install-recommends \
    libopenblas-dev \
    && apt-get clean \ 
    && rm -rf /var/lib/apt/lists/*; \
elif [ "$TARGETARCH" = "amd64" ]; then \
    echo "🖥️ Installing AMD64-specific optimizations"; \
    apt-get update && apt-get install -y --no-install-recommends \
    libomp-dev \
    && apt-get clean \ 
    && rm -rf /var/lib/apt/lists/*; \
else \
    echo "Skipping platform-specific optimizations (unsupported platform)"; \
fi

# Create a non-root user and group
RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser

# Create and set permissions for appuser home directory
RUN mkdir -p /home/appuser && chown -R appuser:appuser /home/appuser

WORKDIR ${APP_HOME}

RUN echo '#!/bin/bash\n\
if [ "$USE_LOCAL" = "true" ]; then\n\
    echo "📦 Installing from local source..."\n\
    pip install --no-cache-dir /tmp/project/\n\
else\n\
    echo "🌐 Installing from GitHub..."\n\
    for i in {1..3}; do \n\
        git clone --branch ${GITHUB_BRANCH} ${GITHUB_REPO} /tmp/crawl4ai && break || \n\
        { echo "Attempt $i/3 failed! Taking a short break... ☕"; sleep 5; }; \n\
    done\n\
    pip install --no-cache-dir /tmp/crawl4ai\n\
fi' > /tmp/install.sh && chmod +x /tmp/install.sh

COPY . /tmp/project/

# Copy supervisor config first (might need root later, but okay for now)
COPY deploy/docker/supervisord.conf .

COPY deploy/docker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN if [ "$INSTALL_TYPE" = "all" ] ; then \
        pip install --no-cache-dir \
            torch \
            torchvision \
            torchaudio \
            scikit-learn \
            nltk \
            transformers \
            tokenizers && \
        python -m nltk.downloader punkt stopwords ; \
    fi

RUN if [ "$INSTALL_TYPE" = "all" ] ; then \
        pip install "/tmp/project/[all]" && \
        python -m crawl4ai.model_loader ; \
    elif [ "$INSTALL_TYPE" = "torch" ] ; then \
        pip install "/tmp/project/[torch]" ; \
    elif [ "$INSTALL_TYPE" = "transformer" ] ; then \
        pip install "/tmp/project/[transformer]" && \
        python -m crawl4ai.model_loader ; \
    else \
        pip install "/tmp/project" ; \
    fi

RUN pip install --no-cache-dir --upgrade pip && \
    /tmp/install.sh && \
    python -c "import crawl4ai; print('✅ crawl4ai is ready to rock!')" && \
    python -c "from playwright.sync_api import sync_playwright; print('✅ Playwright is feeling dramatic!')"

RUN crawl4ai-setup

RUN playwright install --with-deps

RUN mkdir -p /home/appuser/.cache/ms-playwright \
    && cp -r /root/.cache/ms-playwright/chromium-* \
        /root/.cache/ms-playwright/chromium_headless_shell-* \
        /home/appuser/.cache/ms-playwright/ \
    && chown -R appuser:appuser /home/appuser/.cache/ms-playwright

RUN crawl4ai-doctor

# Ensure all cache directories belong to appuser
# This fixes permission issues with .cache/url_seeder and other runtime cache dirs
RUN mkdir -p /home/appuser/.cache \
    && chown -R appuser:appuser /home/appuser/.cache

# ── nodriver stealth worker (AGPL-3.0, process-isolated) ────────────────────
# nodriver is AGPL-3.0: it lives ONLY in this worker's own venv and is
# imported ONLY by worker.py (supervisord program "nodriver-worker"). The
# crawl4ai package and the main API never import it — AGPL isolation by
# process + network boundary (separate venv, port 8001 reachable only via an
# explicit -p mapping or the internal docker network).
# The built image therefore contains an AGPL component: keep it private.
# See deploy/docker/nodriver_worker/LICENSE-NOTICE.md.
RUN python3 -m venv /opt/nodriver-worker
COPY deploy/docker/nodriver_worker/requirements.txt /tmp/nodriver-worker-requirements.txt
RUN /opt/nodriver-worker/bin/pip install --no-cache-dir -r /tmp/nodriver-worker-requirements.txt \
    && /opt/nodriver-worker/bin/python - <<'EOF'
# nodriver 0.50.3 ships nodriver/cdp/network.py with a stray cp1252 0xB1 byte
# (a '±' inside a comment) that is invalid UTF-8 and breaks `import nodriver`
# on every Python 3. Fix: encode the ± properly (0xC2 0xB1).
import pathlib

import nodriver

p = pathlib.Path(nodriver.__file__).parent / "cdp" / "network.py"
d = p.read_bytes()
if b"\xb1Inf" in d:
    p.write_bytes(d.replace(b"\xb1Inf", b"\xc2\xb1Inf"))
    print("patched nodriver/cdp/network.py (invalid UTF-8 byte -> U+00B1)")
import nodriver as _n  # noqa: F401  — prove the import works now
print("nodriver import OK")
EOF
COPY deploy/docker/nodriver_worker/worker.py deploy/docker/nodriver_worker/run.sh /opt/nodriver-worker/
# The venv dir doubles as the worker's CWD (supervisord `directory=`): nodriver's
# verify_cf/template_location writes screen.jpg + cf_template.png into the CWD
# and SILENTLY swallows PermissionError (returns None -> TypeError upstream).
# Root-owned CWD therefore disables CF checkbox verification entirely — make it
# appuser-writable. (Subdirs stay root-owned/readable; only the top dir is written.)
RUN chmod +x /opt/nodriver-worker/run.sh && chown appuser:appuser /opt/nodriver-worker

# Copy application code
COPY deploy/docker/* ${APP_HOME}/

# copy the playground + any future static assets
COPY deploy/docker/static ${APP_HOME}/static

# LLM reasoning-effort hook (Qwen3.8 thinking control): the .pth auto-imports
# c4ai_llm_thinking at interpreter startup, wrapping LLMContentFilter.__init__
# to inject extra_body. No-op unless LLM_REASONING_EFFORT is set.
COPY deploy/docker/llm-hook/c4ai_llm_thinking.py /usr/local/lib/python3.12/site-packages/c4ai_llm_thinking.py
COPY deploy/docker/llm-hook/zz_c4ai_llm.pth /usr/local/lib/python3.12/site-packages/zz_c4ai_llm.pth

# /app is root-owned and read-only to the runtime user: a write bug can no
# longer plant a persistent self-RCE in the application directory.
RUN chown -R root:root ${APP_HOME} && chmod -R a-w ${APP_HOME}

# give permissions to redis persistence dirs if used
RUN mkdir -p /var/lib/redis /var/log/redis && chown -R appuser:appuser /var/lib/redis /var/log/redis

# Sandboxed artifact store (server-owned screenshot/PDF outputs), 0700.
RUN mkdir -p /var/lib/crawl4ai/outputs \
    && chown -R appuser:appuser /var/lib/crawl4ai \
    && chmod 700 /var/lib/crawl4ai/outputs

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD bash -c '\
    MEM=$(free -m | awk "/^Mem:/{print \$2}"); \
    if [ $MEM -lt 2048 ]; then \
        echo "⚠️ Warning: Less than 2GB RAM available! Your container might need a memory boost! 🚀"; \
        exit 1; \
    fi && \
    redis-cli ping > /dev/null && \
    curl -f http://localhost:11235/health || exit 1'

# Redis is in-container only (loopback + requirepass); never expose its port.
# (was: EXPOSE 6379)
# nodriver stealth worker: container-internal on port 8001 (not publicly
# exposed); map it to a host port only if the operator wants to reach it.
EXPOSE 8001
# Switch to the non-root user before starting the application
USER appuser

# Set environment variables to ptoduction
ENV PYTHON_ENV=production 

# Start via entrypoint.sh, which resolves the socket-level auth/egress posture
# (loopback unless a credential is present) and the redis password, then execs
# supervisord.
CMD ["bash", "entrypoint.sh"]
