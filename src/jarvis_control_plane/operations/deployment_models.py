"""Static deployment contract values and report models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ServiceResourceLimits:
    memory: str
    cpus: Decimal
    pids: int


REQUIRED_FILES = (
    "Dockerfile",
    "README.md",
    "artifacts.lock.json",
    "compose.yaml",
    "config.example.toml",
    "health_probe.py",
    "openwa-handoff.md",
    "requirements.lock",
    "systemd/jarvis-backup.service",
    "systemd/jarvis-backup.timer",
    "systemd/jarvis-ubuntu-worker.service",
    "windows/install-jarvis-worker.ps1",
)

DATABASE_SCHEMAS = MappingProxyType(
    {
        "state": "4c4e03d8f879ad235051543caa4ef7782f408c05953087d5cf0201c261a59c43",
        "sessions": "702f19c90b7c336532f4a7e598801150ac9f68bc3471b5d2b0e69317eb974470",
        "audit": "07918a1e796be9ed5f0c720fd490ca59354e39742eee2b9e77769f6ec1702648",
        "traces": "c20e4c17acc056d1ea5ceb2723c607ff1c42c99febe2d6b4759863633cc47dbd",
        "google_traces": "c20e4c17acc056d1ea5ceb2723c607ff1c42c99febe2d6b4759863633cc47dbd",
        "deleted_conversations": "fb1b292ce25216b5f697aba99a90a83f77dbc6aba755c61ce69488748bef066d",
    }
)

RESOURCE_LIMITS: Mapping[str, ServiceResourceLimits] = MappingProxyType(
    {
        "inbound_receiver": ServiceResourceLimits("64M", Decimal("0.10"), 32),
        "capability_broker": ServiceResourceLimits("144M", Decimal("0.25"), 32),
        "orchestration_agent": ServiceResourceLimits("224M", Decimal("0.42"), 112),
        "audit_service": ServiceResourceLimits("64M", Decimal("0.10"), 32),
        "google_connector": ServiceResourceLimits("64M", Decimal("0.12"), 48),
        "knowledge_vault_connector": ServiceResourceLimits("96M", Decimal("0.17"), 48),
        "openwa_outbound_connector": ServiceResourceLimits("64M", Decimal("0.10"), 32),
        "worker_gateway": ServiceResourceLimits("96M", Decimal("0.25"), 64),
        "public_oauth_callback": ServiceResourceLimits("48M", Decimal("0.10"), 32),
        "deleted_conversation_archive": ServiceResourceLimits(
            "48M", Decimal("0.10"), 32
        ),
        "orchestration_egress_proxy": ServiceResourceLimits("48M", Decimal("0.06"), 16),
        "google_egress_proxy": ServiceResourceLimits("48M", Decimal("0.06"), 16),
        "vault_egress_proxy": ServiceResourceLimits("48M", Decimal("0.06"), 16),
    }
)

EXPECTED_IDENTITIES: Mapping[str, str] = MappingProxyType(
    {
        "inbound_receiver": "jarvis-inbound",
        "capability_broker": "jarvis-broker",
        "orchestration_agent": "jarvis-orchestration",
        "audit_service": "jarvis-audit",
        "google_connector": "jarvis-google",
        "knowledge_vault_connector": "jarvis-vault",
        "openwa_outbound_connector": "jarvis-openwa-outbound",
        "worker_gateway": "jarvis-worker-gateway",
        "public_oauth_callback": "jarvis-oauth-callback",
        "deleted_conversation_archive": "jarvis-deleted-archive",
        "orchestration_egress_proxy": "jarvis-orchestration-egress",
        "google_egress_proxy": "jarvis-google-egress",
        "vault_egress_proxy": "jarvis-vault-egress",
    }
)

ALLOWED_CREDENTIAL_MOUNTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "inbound_receiver": frozenset({"/run/credentials/openwa-inbound"}),
        "capability_broker": frozenset({"/run/credentials/broker"}),
        "orchestration_agent": frozenset({"/run/credentials/openai"}),
        "audit_service": frozenset(),
        "google_connector": frozenset({"/run/credentials/google"}),
        "knowledge_vault_connector": frozenset({"/run/credentials/vault"}),
        "openwa_outbound_connector": frozenset({"/run/credentials/openwa"}),
        "worker_gateway": frozenset({"/run/credentials/windows-worker"}),
        "public_oauth_callback": frozenset(),
        "deleted_conversation_archive": frozenset(),
        "orchestration_egress_proxy": frozenset(),
        "google_egress_proxy": frozenset(),
        "vault_egress_proxy": frozenset(),
    }
)

ALLOWED_PROTOCOL_MOUNTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "inbound_receiver": frozenset(
            {"/run/protocol/inbound_receiver--capability_broker.key"}
        ),
        "capability_broker": frozenset(
            {
                "/run/protocol/inbound_receiver--capability_broker.key",
                "/run/protocol/capability_broker--orchestration_agent.key",
                "/run/protocol/capability_broker--audit_service.key",
                "/run/protocol/capability_broker--google_connector.key",
                "/run/protocol/capability_broker--knowledge_vault_connector.key",
                "/run/protocol/capability_broker--openwa_outbound_connector.key",
                "/run/protocol/capability_broker--worker_gateway.key",
                "/run/protocol/capability_broker--deleted_conversation_archive.key",
            }
        ),
        "orchestration_agent": frozenset(
            {
                "/run/protocol/capability_broker--orchestration_agent.key",
                "/run/protocol/orchestration_agent--google_connector.key",
                "/run/protocol/orchestration_agent--knowledge_vault_connector.key",
            }
        ),
        "audit_service": frozenset(
            {
                "/run/protocol/capability_broker--audit_service.key",
                "/run/protocol/google_connector--audit_service.key",
            }
        ),
        "google_connector": frozenset(
            {
                "/run/protocol/capability_broker--google_connector.key",
                "/run/protocol/orchestration_agent--google_connector.key",
                "/run/protocol/public_oauth_callback--google_connector.key",
                "/run/protocol/google_connector--audit_service.key",
            }
        ),
        "knowledge_vault_connector": frozenset(
            {
                "/run/protocol/capability_broker--knowledge_vault_connector.key",
                "/run/protocol/orchestration_agent--knowledge_vault_connector.key",
            }
        ),
        "openwa_outbound_connector": frozenset(
            {"/run/protocol/capability_broker--openwa_outbound_connector.key"}
        ),
        "worker_gateway": frozenset(
            {"/run/protocol/capability_broker--worker_gateway.key"}
        ),
        "public_oauth_callback": frozenset(
            {"/run/protocol/public_oauth_callback--google_connector.key"}
        ),
        "deleted_conversation_archive": frozenset(
            {"/run/protocol/capability_broker--deleted_conversation_archive.key"}
        ),
        "orchestration_egress_proxy": frozenset(),
        "google_egress_proxy": frozenset(),
        "vault_egress_proxy": frozenset(),
    }
)

ALLOWED_STATE_MOUNTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "inbound_receiver": frozenset(),
        "capability_broker": frozenset(
            {
                "/var/lib/jarvis/state",
                "/var/lib/jarvis/traces",
                "/run/jarvis-deleted",
            }
        ),
        "orchestration_agent": frozenset(),
        "audit_service": frozenset({"/var/lib/jarvis/audit"}),
        "google_connector": frozenset({"/var/lib/jarvis/google-traces"}),
        "knowledge_vault_connector": frozenset({"/var/lib/jarvis/vault"}),
        "openwa_outbound_connector": frozenset(),
        "worker_gateway": frozenset(),
        "public_oauth_callback": frozenset(),
        "deleted_conversation_archive": frozenset(
            {
                "/var/lib/jarvis/deleted-conversations",
                "/run/jarvis-deleted",
            }
        ),
        "orchestration_egress_proxy": frozenset(),
        "google_egress_proxy": frozenset(),
        "vault_egress_proxy": frozenset(),
    }
)


class BundleValidationError(ValueError):
    """One or more static deployment invariants failed."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class BundleVerificationReport:
    release_id: str
    services: tuple[str, ...]
    aggregate_memory_mib: int
    aggregate_cpus: float
    aggregate_pids: int
    openwa_handoff_activated: bool
    checked_files: tuple[str, ...]
    host_mutations: tuple[str, ...] = ()
