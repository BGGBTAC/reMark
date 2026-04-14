# syntax=docker/dockerfile:1.7

# --- builder ----------------------------------------------------------------
# Build wheels in an isolated stage so the runtime image stays small and
# free of build toolchains.
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1

# Native deps needed to build numpy, cairosvg, cryptography, reportlab, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libcairo2-dev \
        libffi-dev \
        libjpeg-dev \
        libssl-dev \
        pkg-config \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --upgrade pip && \
    pip wheel --wheel-dir /wheels .

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    REMARK_CONFIG=/config/config.yaml \
    REMARK_LOG_FORMAT=json

# Runtime system libs only (no compilers). cairo is needed by cairosvg;
# git is needed for the Git-backed Obsidian vault sync; tini for signal
# handling so uvicorn/asyncio shutdowns are clean.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        libcairo2 \
        libjpeg62-turbo \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin remark

COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels remark-bridge \
    && rm -rf /wheels

# Directories for state, vault, and config. The compose file mounts
# volumes on top of these.
RUN mkdir -p /config /vault /state /home/remark/.remark-bridge \
    && chown -R remark:remark /config /vault /state /home/remark

USER remark
WORKDIR /home/remark

EXPOSE 8000

# Default command runs the web dashboard. The sync daemon variant is
# selected in docker-compose with `command: sync`.
ENTRYPOINT ["/usr/bin/tini", "--", "remark-bridge"]
CMD ["serve-web", "--host", "0.0.0.0", "--port", "8000"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        r=urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3); \
        sys.exit(0 if r.status==200 else 1)" || exit 1
