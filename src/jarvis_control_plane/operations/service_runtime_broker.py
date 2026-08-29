"""Capability-broker composition and asynchronous inbound admission."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..models import SignedInboundEvent
from ..ports import DurableStateStore


def broker_state_store(state_root: Any, *, runtime: Any) -> Any:
    """Compose durable broker state with its separate write-only archive client."""

    deleted_archive = runtime.SQLiteDeletedConversationArchiveWriter(
        "/run/jarvis-deleted/writer.sock",
        authkey=runtime._read_secret(
            runtime.Path("/run/protocol")
            / "capability_broker--deleted_conversation_archive.key"
        ),
    )
    return runtime.SQLiteDurableStateStore(
        state_root / "state.sqlite3", deleted_archive=deleted_archive
    )


class AsyncIngressAdmission:
    """Acknowledge durable admission while a background worker drains ingress."""

    def __init__(
        self,
        *,
        receiver: Any,
        state: DurableStateStore,
        runtime: Any,
    ) -> None:
        self._runtime = runtime
        self._receiver = receiver
        self._state = state
        self._worker = runtime.OpenWAIngressWorker(receiver=receiver, state=state)
        self._wakeup = runtime.Event()
        self._controls: Any = runtime.Queue()
        runtime.Thread(target=self._drain, daemon=True).start()
        runtime.Thread(target=self._drain_controls, daemon=True).start()

    def receive(self, event: SignedInboundEvent) -> object:
        result = self._receiver.admit(event)
        if result.disposition == "admitted":
            message = event.decode()
            if isinstance(message.text, str) and self._runtime.parse_control(
                message.text
            ).command in {
                self._runtime.ControlCommand.STATUS,
                self._runtime.ControlCommand.CANCEL,
                self._runtime.ControlCommand.NEW,
            }:
                self._controls.put((message, None))
            self._wakeup.set()
        return result

    def _drain_controls(self) -> None:
        while True:
            message, disposition = self._controls.get()
            try:
                if disposition is None:
                    if not self._state.begin_ingress_dispatch(
                        transport_session_id=message.session_id,
                        message_id=message.message_id,
                    ):
                        continue
                    try:
                        self._receiver.dispatch_admitted_message(message)
                    except Exception:  # noqa: BLE001 - preserve interrupted ingress
                        disposition = "interrupted"
                    else:
                        disposition = "dispatched"
                self._state.finish_ingress_dispatch(
                    transport_session_id=message.session_id,
                    message_id=message.message_id,
                    disposition=disposition,
                )
            except Exception:  # noqa: BLE001 - retain durable terminalization
                self._runtime.sleep(0.1)
                self._controls.put((message, disposition))

    def _drain(self) -> None:
        while True:
            self._wakeup.wait()
            self._wakeup.clear()
            try:
                while self._worker.run_once() is not None:
                    pass
            except Exception:  # noqa: BLE001 - isolate one interrupted ingress item
                self._wakeup.set()


def audit_operations(
    config: Mapping[str, Any], *, runtime: Any
) -> Mapping[str, Callable[..., object]]:
    paths = config.get("paths")
    if not isinstance(paths, Mapping):
        raise runtime.CompositionError("configuration paths are unavailable")
    root = runtime.Path(runtime._require_text(paths.get("audit"), "paths.audit"))
    root.mkdir(parents=True, exist_ok=True)
    audit = runtime.SQLiteAuditBoundary(root / "audit.sqlite3")
    return {
        "append": audit.append,
        "append_batch": audit.append_batch,
        "writable": audit.writable,
        "safe_view": audit.safe_view,
        "export_json": audit.export_json,
    }


def broker_operations(
    config: Mapping[str, Any], *, runtime: Any
) -> Mapping[str, Callable[..., object]]:
    deployment = config.get("deployment")
    paths = config.get("paths")
    model_config = config.get("models")
    if not isinstance(deployment, Mapping):
        raise runtime.CompositionError("broker deployment configuration is incomplete")
    if not isinstance(paths, Mapping):
        raise runtime.CompositionError("broker path configuration is incomplete")
    if not isinstance(model_config, Mapping):
        raise runtime.CompositionError("broker model configuration is incomplete")

    state_root = runtime.Path(runtime._require_text(paths.get("state"), "paths.state"))
    trace_root = runtime.Path(
        runtime._require_text(paths.get("traces"), "paths.traces")
    )
    state_root.mkdir(parents=True, exist_ok=True)
    trace_root.mkdir(parents=True, exist_ok=True)
    clock = runtime.SystemClock()
    ids = runtime.UuidIdGenerator()
    state = runtime._broker_state_store(state_root)
    audit = runtime.RemoteAuditBoundary(
        runtime._client(
            config, client_identity="jarvis-broker", server_role="audit_service"
        )
    )
    orchestration = runtime.RemoteOrchestrationAdapter(
        runtime._client(
            config,
            client_identity="jarvis-broker",
            server_role="orchestration_agent",
        )
    )
    outbound_client = runtime._client(
        config,
        client_identity="jarvis-broker",
        server_role="openwa_outbound_connector",
    )
    google_client = runtime._client(
        config,
        client_identity="jarvis-broker",
        server_role="google_connector",
    )
    google_actions = runtime.RemoteActionDispatcher(google_client, bound=True)
    vault_client = runtime._client(
        config,
        client_identity="jarvis-broker",
        server_role="knowledge_vault_connector",
    )
    vault_actions = runtime.RemoteActionDispatcher(vault_client, bound=True)
    worker_client = runtime._client(
        config, client_identity="jarvis-broker", server_role="worker_gateway"
    )
    worker_actions = runtime.RemoteActionDispatcher(worker_client)
    actions = runtime.RoutedActionDispatcher(
        terminal=worker_actions,
        gmail=google_actions,
        gmail_lifecycle=google_actions,
        vault=vault_actions,
        vault_lifecycle=vault_actions,
    )
    trace_store = runtime.SQLiteDiagnosticTraceStore(
        trace_root / "traces.sqlite3",
        minimum_free_bytes=runtime._minimum_free_bytes(config),
    )
    trace = runtime.DiagnosticTraceRecorder(
        writer=trace_store.writer(), clock=clock, ids=ids
    )
    allowed_models = model_config.get("allowed_models")
    allowed_reasoning = model_config.get("allowed_reasoning")
    if not isinstance(allowed_models, list) or not isinstance(allowed_reasoning, list):
        raise runtime.CompositionError("model availability configuration is invalid")
    broker_credential = runtime._credential_json(
        runtime.Path("/run/credentials/broker/credentials.json")
    )
    control_config = runtime.ControlPlaneConfig(
        operator_id=runtime._require_text(deployment.get("operator_id"), "operator_id"),
        session_id=runtime._require_text(
            deployment.get("openwa_internal_session_id"),
            "openwa_internal_session_id",
        ),
        signing_secret=runtime._require_text(
            broker_credential.get("openwa_signing_secret"), "OpenWA signing secret"
        ).encode(),
    )
    broker = runtime.DeterministicCapabilityBroker(
        config=control_config,
        state=state,
        audit=audit,
        orchestration=orchestration,
        outbound=runtime.RemoteOutboundConnector(outbound_client),
        clock=clock,
        ids=ids,
        trace=trace,
        model_availability_provider=runtime.FixedModelAvailabilityProvider(
            runtime.ModelAvailability(
                available_models=tuple(allowed_models),
                available_reasoning_levels=tuple(allowed_reasoning),
            )
        ),
        messaging_readiness_provider=runtime.RemoteMessagingReadinessProvider(
            outbound_client
        ),
        worker_readiness_provider=runtime.RemoteWorkerReadinessProvider(worker_client),
        google_readiness_provider=runtime.RemoteGoogleReadinessProvider(google_client),
        working_sessions=runtime.SQLiteWorkingSessionStore(
            state_root / "sessions.sqlite3"
        ),
        action_dispatcher=actions,
        action_lifecycle=actions,
        vault_write_proposal_preparer=runtime.RemoteVaultProposalPreparer(vault_client),
    )
    receiver = runtime.SignedMessageReceiver(
        config=control_config,
        state=state,
        audit=audit,
        broker=broker,
        clock=clock,
        ids=ids,
    )
    ingress = runtime._AsyncIngressAdmission(receiver=receiver, state=state)
    return {"receive": ingress.receive}
