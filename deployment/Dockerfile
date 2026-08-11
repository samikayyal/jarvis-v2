FROM ghcr.io/astral-sh/uv:0.6.14@sha256:3362a526af7eca2fcd8604e6a07e873fb6e4286d8837cb753503558ce1213664 AS uv
FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f

ARG GIT_VERSION=1:2.39.5-0+deb12u3
ARG OPENSSH_CLIENT_VERSION=1:9.2p1-2+deb12u10
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        "git=${GIT_VERSION}" \
        "openssh-client=${OPENSSH_CLIENT_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/jarvis/src

WORKDIR /opt/jarvis
COPY --from=uv /uv /uvx /bin/
RUN install -d -m 0700 -o 10002 -g 10002 \
        /var/lib/jarvis/state /var/lib/jarvis/deleted-conversations \
    && install -d -m 0700 -o 10002 -g 10002 /var/lib/jarvis/traces \
    && install -d -m 0700 -o 10005 -g 10005 /var/lib/jarvis/google-traces \
    && install -d -m 0700 -o 10004 -g 10004 /var/lib/jarvis/audit \
    && install -d -m 0700 -o 10006 -g 10006 /var/lib/jarvis/vault \
    && install -d -m 0700 -o 10008 -g 10008 /run/jarvis-worker
COPY deployment/requirements.lock /opt/jarvis/deployment/requirements.lock
RUN uv pip install --system --no-cache --require-hashes \
    -r /opt/jarvis/deployment/requirements.lock

COPY pyproject.toml README.md /opt/jarvis/
COPY src /opt/jarvis/src
COPY deployment /opt/jarvis/deployment
RUN uv run --no-project python -m jarvis_control_plane.deployment \
    /opt/jarvis/deployment --source-root /opt/jarvis

ENTRYPOINT ["uv", "run", "--no-project", "python", "-m", "jarvis_control_plane.service_runtime"]
