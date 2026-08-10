FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/jarvis/src

WORKDIR /opt/jarvis
COPY deployment/requirements.lock /opt/jarvis/deployment/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes \
    -r /opt/jarvis/deployment/requirements.lock

COPY src /opt/jarvis/src
COPY deployment /opt/jarvis/deployment

ENTRYPOINT ["python", "-m", "jarvis_control_plane.deployment"]
CMD ["/opt/jarvis/deployment"]
