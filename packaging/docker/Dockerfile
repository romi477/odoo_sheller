# The daemon, containerized. Docker-outside-of-Docker: no Engine in here, just
# the CLI talking to the host's socket — the same thing the native daemon does,
# started from somewhere else.
#
# Not a PyInstaller freeze. Freezing answers "this machine has no Python and a
# minimal PATH", and a container has neither problem by construction; it would
# only add hidden-import failures.
ARG UV_VERSION=0.5.11

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:3.12-slim-bookworm

# The static CLI tarball, not docker-ce-cli: that package would bring an apt
# repository and a signing key for the sake of one file.
ARG DOCKER_VERSION=27.5.1
ARG TARGETARCH

COPY --from=uv /uv /usr/local/bin/uv

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gosu; \
    case "$TARGETARCH" in \
        amd64) docker_arch=x86_64 ;; \
        arm64) docker_arch=aarch64 ;; \
        *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_VERSION}.tgz" \
        | tar -xz -C /usr/local/bin --strip-components=1 docker/docker; \
    docker --version; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# HOME is the whole mechanism: journal.py, registry.py and transport.py build
# their paths from Path.home(), so this puts the journals, the admin key and
# the SSH control sockets under the bind mount without touching the code.
ENV HOME=/data \
    PATH=/app/.venv/bin:$PATH \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    ODOO_SHELLER_IN_CONTAINER=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
COPY odoo_sheller ./odoo_sheller
RUN uv sync --frozen --no-dev

COPY packaging/docker/entrypoint.sh /usr/local/bin/odoo-sheller-entrypoint
RUN chmod +x /usr/local/bin/odoo-sheller-entrypoint && mkdir -p /data

# Metadata for the default port. ODOO_SHELLER_PORT moves the listener, and
# then the -p mapping has to name the new one; EXPOSE does not affect either.
EXPOSE 8765

# There is no dedicated health endpoint. This one is cheap and answers only
# once the application is actually serving. It reads the port rather than
# assuming it: a hardcoded 8765 would report a working container unhealthy
# forever the moment somebody set ODOO_SHELLER_PORT.
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:%s/api/sessions" % os.environ.get("ODOO_SHELLER_PORT", "8765"), timeout=2)'

ENTRYPOINT ["/usr/local/bin/odoo-sheller-entrypoint"]
