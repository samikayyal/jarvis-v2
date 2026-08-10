FROM ghcr.io/astral-sh/uv:0.6.14@sha256:3362a526af7eca2fcd8604e6a07e873fb6e4286d8837cb753503558ce1213664 AS uv
FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/jarvis/src

WORKDIR /opt/jarvis
COPY --from=uv /uv /uvx /bin/
COPY deployment/requirements.lock /opt/jarvis/deployment/requirements.lock
RUN uv pip install --system --no-cache --require-hashes \
    -r /opt/jarvis/deployment/requirements.lock

COPY pyproject.toml README.md /opt/jarvis/
COPY src /opt/jarvis/src
COPY deployment /opt/jarvis/deployment
RUN uv run --no-project python -m jarvis_control_plane.deployment \
    /opt/jarvis/deployment --source-root /opt/jarvis

ENTRYPOINT ["uv", "run", "--no-project", "python", "-m", "jarvis_control_plane.deployment"]
CMD ["/opt/jarvis/deployment"]
