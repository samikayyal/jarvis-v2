# Jarvis Assistant V1 unactivated bundle

This directory is a reviewed, offline-verifiable deployment description. Every
container is behind the `manual-activation` Compose profile. Running the verifier
does not build or start a container, install either native worker, provision a
credential, create or attach a network, change a firewall, or contact OpenWA.

Verify from the repository root:

```text
uv run python -m jarvis_control_plane.deployment deployment
docker compose --file deployment/compose.yaml config --quiet
```

The second command parses Compose only. Do not pass `--profile
manual-activation` or run `up` during isolated verification.

`config.example.json` contains no real operator, Google, OpenWA, vault, or worker
identity. Before a later supervised activation, a manual administrator must make
a root-owned `0444` configuration from the reviewed schema, create only the
service-specific credential directories described below, and independently
review the resulting exact values. Credential material never belongs in this
repository.

| Service | Credential boundary |
| --- | --- |
| orchestration agent | `/run/credentials/openai` read-only |
| Google connector | `/run/credentials/google` private writable directory |
| knowledge-vault connector | `/run/credentials/vault` read-only |
| OpenWA outbound connector | `/run/credentials/openwa` read-only |
| worker gateway | `/run/credentials/windows-worker` read-only |

The native Ubuntu and Windows workers are not Compose services. This bundle does
not install them, register identities, expose listeners, provision certificates,
or alter the host. OpenWA is not a service in this Compose project; only its
future handoff is documented in `openwa-handoff.md`.

The image build is reproducible from the Git-pinned application artifact, the
digest-pinned Python base, and the hash-locked exported requirements. Building,
publishing, credential provisioning, worker installation, OpenWA attachment,
and activation are separate manual-administration steps.
