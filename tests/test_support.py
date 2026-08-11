"""Shared controlled receiver builder for the Ticket 01–06 test seams."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jarvis_control_plane import (
    ActionDispatcher,
    BoundActionLifecycle,
    ControlledActionDispatcher,
    ControlledOrchestrationAdapter,
    ControlledOutboundConnector,
    ControlPlaneConfig,
    DeterministicCapabilityBroker,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    FixedModelAvailabilityProvider,
    InMemoryAuditBoundary,
    InMemoryDiagnosticTraceStore,
    InMemoryDurableStateStore,
    ModelAvailability,
    SignedMessageReceiver,
)
from jarvis_control_plane.ports import (
    MessagingGatewayReadinessProvider,
    OutboundConnector,
    WorkerReadinessProvider,
)


@dataclass(frozen=True, slots=True)
class ReceiverComponents:
    """One controlled receiver graph with all test-visible collaborators."""

    config: ControlPlaneConfig
    state: Any
    audit: Any
    clock: FixedClock
    ids: DeterministicIdGenerator
    provider: FixedModelAvailabilityProvider
    orchestration: ControlledOrchestrationAdapter
    outbound: OutboundConnector
    broker: DeterministicCapabilityBroker
    receiver: SignedMessageReceiver
    trace_store: InMemoryDiagnosticTraceStore | None
    trace: DiagnosticTraceRecorder
    action_dispatcher: ActionDispatcher
    action_lifecycle: BoundActionLifecycle | None


def build_receiver_components(
    *,
    operator_id: str,
    transport_session_id: str,
    signing_secret: bytes,
    now: datetime,
    id_prefix: str,
    state: Any | None = None,
    audit: Any | None = None,
    orchestration: ControlledOrchestrationAdapter | None = None,
    outbound: OutboundConnector | None = None,
    messaging_readiness_provider: MessagingGatewayReadinessProvider | None = None,
    worker_readiness_provider: WorkerReadinessProvider | None = None,
    availability: ModelAvailability | None = None,
    working_session_id: str | None = None,
    clock: FixedClock | None = None,
    ids: DeterministicIdGenerator | None = None,
    trace: DiagnosticTraceRecorder | None = None,
    action_dispatcher: ActionDispatcher | None = None,
    action_lifecycle: BoundActionLifecycle | None = None,
    vault_write_proposal_preparer: Any | None = None,
    working_sessions: Any | None = None,
) -> ReceiverComponents:
    """Build the repeated receiver/broker graph while preserving test overrides."""

    config = ControlPlaneConfig(
        operator_id=operator_id,
        session_id=transport_session_id,
        signing_secret=signing_secret,
        working_session_id=working_session_id,
    )
    clock = clock or FixedClock(now)
    ids = ids or DeterministicIdGenerator(id_prefix)
    state = state if state is not None else InMemoryDurableStateStore()
    audit = audit if audit is not None else InMemoryAuditBoundary()
    orchestration = orchestration or ControlledOrchestrationAdapter()
    provider = FixedModelAvailabilityProvider(availability or ModelAvailability())
    outbound = (
        outbound
        if outbound is not None
        else ControlledOutboundConnector(
            operator_id=operator_id,
            session_id=transport_session_id,
            audit=audit,
            clock=clock,
            ids=ids,
        )
    )
    trace_store = None
    if trace is None:
        trace_store = InMemoryDiagnosticTraceStore()
        trace = DiagnosticTraceRecorder(
            writer=trace_store.writer(), clock=clock, ids=ids
        )
    action_dispatcher = action_dispatcher or ControlledActionDispatcher()
    broker = DeterministicCapabilityBroker(
        config=config,
        state=state,
        audit=audit,
        orchestration=orchestration,
        outbound=outbound,
        clock=clock,
        ids=ids,
        trace=trace,
        model_availability_provider=provider,
        messaging_readiness_provider=messaging_readiness_provider,
        worker_readiness_provider=worker_readiness_provider,
        action_dispatcher=action_dispatcher,
        action_lifecycle=action_lifecycle,
        vault_write_proposal_preparer=vault_write_proposal_preparer,
        working_sessions=working_sessions,
    )
    receiver = SignedMessageReceiver(
        config=config,
        state=state,
        audit=audit,
        broker=broker,
        clock=clock,
        ids=ids,
    )
    return ReceiverComponents(
        config=config,
        state=state,
        audit=audit,
        clock=clock,
        ids=ids,
        provider=provider,
        orchestration=orchestration,
        outbound=outbound,
        broker=broker,
        receiver=receiver,
        trace_store=trace_store,
        trace=trace,
        action_dispatcher=action_dispatcher,
        action_lifecycle=action_lifecycle,
    )
