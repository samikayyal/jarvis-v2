# Jarvis personal runtime

Jarvis is a single-operator personal assistant that runs as one native Python
service on Ubuntu. A separately operated OpenWA messaging gateway hands direct
WhatsApp messages to the runtime, which admits only text from the configured
authorized operator. Deterministic slash commands are handled locally; other
admitted text becomes an ordinary request in a sequential OpenAI Responses
model-and-tool loop.

The active runtime is intentionally small:

- `src/jarvis_personal_runtime/` contains the complete assistant runtime.
- `tests/personal_runtime/` contains its surviving automated contract.
- `deployment/personal-runtime/` contains the native `systemd` package,
  configuration examples, pinned requirements, and operations guide.
- `docs/openwa/` documents the independent messaging gateway.
- `.scratch/` retains the project history, research, issues, and acceptance
  evidence. It is not runtime code.

The prepared tools are `read_vault` and `run_terminal`. Ubuntu commands run as
local subprocesses. Windows commands use ordinary OpenSSH over Tailscale. Simple
configured read-only prefixes may run automatically; every other command waits
for the operator's exact approval or a matching saved host-plus-prefix rule.

## Development

This is a Python 3.13 project managed with `uv`.

```console
uv sync
uv run pytest tests/personal_runtime
uv run ruff check src/jarvis_personal_runtime tests/personal_runtime
uv run ruff format --check src/jarvis_personal_runtime tests/personal_runtime
```

The service entry point is:

```console
uv run jarvis-personal-runtime --root /path/to/runtime-root --check
uv run jarvis-personal-runtime --root /path/to/runtime-root
```

The runtime root must contain `.env`, `jarvis.toml`, and `SYSTEM.md`. Keep
credentials out of Git and shell output. See
[`deployment/personal-runtime/README.md`](deployment/personal-runtime/README.md)
for installation, configuration, private OpenWA handoff, operation, update, and
recovery procedures.

## Boundaries

OpenWA remains a distinct messaging gateway with its own container, data volume,
pairing state, readiness contract, and runbooks. Assistant deployment must never
recreate, re-pair, migrate, or expose OpenWA. Container health is necessary but
not sufficient: the configured named session must also be `ready`.

Jarvis accepts only direct text from the configured authorized WhatsApp number.
Groups, other senders, self-authored traffic, media, and malformed events do not
enter assistant work. The working session is memory-only; the seven-day
message-ID cache, saved permissions, and rotating verbatim JSON Lines trace are
the only runtime-owned durable state.

Canonical domain language and trust boundaries live in [`CONTEXT.md`](CONTEXT.md).
