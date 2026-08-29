"""Remote adapters built on the authenticated service protocol."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Mapping, Sequence
from threading import RLock
from urllib.error import URLError
from urllib.request import urlopen

from .. import models
from ..models import AuditEvidence, AuditFilter
from ..ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcher,
    ActionDispatcherError,
    ActionDispatchHandle,
    ActionFinalizer,
    AuditBoundary,
    AuditWriteError,
    BoundActionLifecycle,
    ConnectedServiceReadinessProvider,
    KnowledgeVaultWriteProposalPreparer,
    MessagingGatewayReadiness,
    MessagingGatewayReadinessProvider,
    OrchestrationAdapter,
    OrchestrationAdapterError,
    OutboundConnector,
    OutboundConnectorError,
    WorkerReadiness,
    WorkerReadinessProvider,
)
from ..sessions import ServiceReadiness
from .service_protocol import (
    AuthenticatedServiceClient,
    RemoteServiceError,
    ServiceProtocolError,
)


class RemoteAuditBoundary(AuditBoundary):
    """Audit port adapter backed by the authenticated owned-service seam."""

    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def append(self, evidence: AuditEvidence) -> None:
        try:
            self._client.call("append", evidence)
        except ServiceProtocolError as exc:
            raise AuditWriteError("audit service is unavailable") from exc

    def append_batch(self, evidence: Sequence[AuditEvidence]) -> None:
        try:
            self._client.call("append_batch", tuple(evidence))
        except ServiceProtocolError as exc:
            raise AuditWriteError("audit service is unavailable") from exc

    def safe_view(self, query: AuditFilter | None = None) -> tuple[AuditEvidence, ...]:
        result = self._client.call("safe_view", query)
        if not isinstance(result, tuple) or not all(
            isinstance(item, AuditEvidence) for item in result
        ):
            raise ServiceProtocolError("audit service returned an invalid safe view")
        return result

    def export_json(self, query: AuditFilter | None = None) -> str:
        result = self._client.call("export_json", query)
        if not isinstance(result, str):
            raise ServiceProtocolError("audit service returned an invalid export")
        return result


class RemoteOrchestrationAdapter(OrchestrationAdapter):
    """Planner port adapter backed by the authenticated owned-service seam."""

    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def run(self, request: models.OrchestrationRequest) -> models.OrchestrationResult:
        try:
            result = self._client.call("run", request)
        except RemoteServiceError as exc:
            raise OrchestrationAdapterError(str(exc), code=exc.code) from exc
        except ServiceProtocolError as exc:
            raise OrchestrationAdapterError(str(exc)) from exc
        if not isinstance(result, models.OrchestrationResult):
            raise OrchestrationAdapterError(
                "orchestration service returned an invalid result"
            )
        return result

    def cancel(self, *, request_id: str) -> bool:
        try:
            result = self._client.call("cancel", request_id=request_id)
        except ServiceProtocolError as exc:
            raise OrchestrationAdapterError(str(exc)) from exc
        if not isinstance(result, bool):
            raise OrchestrationAdapterError(
                "orchestration service returned an invalid cancellation result"
            )
        return result


class RemoteOutboundConnector(OutboundConnector):
    """Outbound port adapter backed by the authenticated owned-service seam."""

    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def preflight(self, reply: models.OutboundReply) -> None:
        try:
            self._client.call("preflight", reply)
        except ServiceProtocolError as exc:
            raise OutboundConnectorError(str(exc), may_have_sent=False) from exc

    def send(self, reply: models.OutboundReply) -> models.OutboundDelivery:
        try:
            result = self._client.call("send", reply)
        except ServiceProtocolError as exc:
            may_have_sent = (
                exc.may_have_sent if isinstance(exc, RemoteServiceError) else True
            )
            raise OutboundConnectorError(str(exc), may_have_sent=may_have_sent) from exc
        if not isinstance(result, models.OutboundDelivery):
            raise OutboundConnectorError(
                "outbound service returned an invalid delivery",
                may_have_sent=True,
            )
        return result


class RemoteMessagingReadinessProvider(MessagingGatewayReadinessProvider):
    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def current(self) -> MessagingGatewayReadiness:
        try:
            result = self._client.call("current")
        except ServiceProtocolError as exc:
            raise OutboundConnectorError(str(exc), may_have_sent=False) from exc
        if not isinstance(result, MessagingGatewayReadiness):
            raise OutboundConnectorError(
                "messaging service returned invalid readiness", may_have_sent=False
            )
        return result


class RemoteWorkerReadinessProvider(WorkerReadinessProvider):
    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def current(self) -> WorkerReadiness:
        try:
            result = self._client.call("current")
        except ServiceProtocolError as exc:
            raise ActionDispatcherError("worker readiness is unavailable") from exc
        if not isinstance(result, WorkerReadiness):
            raise ActionDispatcherError("worker service returned invalid readiness")
        return result


class RemoteGoogleReadinessProvider(ConnectedServiceReadinessProvider):
    """Read the Google connector's safe readiness projection over the protocol."""

    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def current(self) -> ServiceReadiness:
        try:
            result = self._client.call("current")
        except ServiceProtocolError as exc:
            raise RuntimeError("Google readiness is unavailable") from exc
        if not isinstance(result, ServiceReadiness) or result.service_id != "google":
            raise RuntimeError("Google service returned invalid readiness")
        return result


class OwnedActionService:
    """Keep prepared connector handles inside their owning service process."""

    def __init__(self, dispatcher: ActionDispatcher) -> None:
        self._dispatcher = dispatcher
        self._prepared: dict[str, ActionDispatchHandle] = {}
        self._lock = RLock()

    def operations(self) -> Mapping[str, Callable[..., object]]:
        operations: dict[str, Callable[..., object]] = {
            "action_prepare": self.prepare,
            "action_run": self.run,
            "action_cancel": self.cancel,
            "action_finalize": self.finalize,
        }
        if isinstance(self._dispatcher, BoundActionLifecycle):
            operations["action_bind"] = self._dispatcher.bind_proposal
            operations["action_validate"] = self._dispatcher.validate_pending_action
        return operations

    def prepare(self, action: models.FrozenActionProposal) -> None:
        with self._lock:
            if action.action_id in self._prepared:
                raise ActionDispatcherError(
                    "action is already prepared", may_have_dispatched=True
                )
            self._prepared[action.action_id] = self._dispatcher.prepare(action)

    def run(self, action_id: str) -> object | None:
        with self._lock:
            handle = self._prepared.get(action_id)
        if handle is None:
            raise ActionDispatcherError(
                "prepared action is unavailable", may_have_dispatched=True
            )
        return handle.run()

    def cancel(self, action_id: str) -> ActionCancellationResult:
        return self._dispatcher.cancel(action_id=action_id)

    def finalize(self, action_id: str) -> None:
        with self._lock:
            self._prepared.pop(action_id, None)
        if isinstance(self._dispatcher, ActionFinalizer):
            self._dispatcher.finalize(action_id=action_id)


class _RemoteActionHandle(ActionDispatchHandle):
    def __init__(self, client: AuthenticatedServiceClient, action_id: str) -> None:
        self._client = client
        self._action_id = action_id

    def run(self) -> object | None:
        try:
            return self._client.call("action_run", self._action_id)
        except ServiceProtocolError as exc:
            may_have_dispatched = (
                exc.may_have_dispatched if isinstance(exc, RemoteServiceError) else True
            )
            raise ActionDispatcherError(
                str(exc), may_have_dispatched=may_have_dispatched
            ) from exc

    def cancel(self) -> ActionCancellationResult:
        try:
            result = self._client.call("action_cancel", self._action_id)
        except (RemoteServiceError, ServiceProtocolError):
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        return (
            result
            if isinstance(result, ActionCancellationResult)
            else ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        )


class RemoteActionDispatcher(ActionDispatcher, BoundActionLifecycle, ActionFinalizer):
    """Prepared action port whose execution handle remains in the owner process."""

    def __init__(
        self, client: AuthenticatedServiceClient, *, bound: bool = False
    ) -> None:
        self._client = client
        self._bound = bound

    def prepare(self, action: models.FrozenActionProposal) -> ActionDispatchHandle:
        try:
            self._client.call("action_prepare", action)
        except ServiceProtocolError as exc:
            may_have_dispatched = (
                exc.may_have_dispatched if isinstance(exc, RemoteServiceError) else True
            )
            raise ActionDispatcherError(
                str(exc), may_have_dispatched=may_have_dispatched
            ) from exc
        return _RemoteActionHandle(self._client, action.action_id)

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        return _RemoteActionHandle(self._client, action_id).cancel()

    def finalize(self, *, action_id: str) -> None:
        try:
            self._client.call("action_finalize", action_id)
        except ServiceProtocolError:
            return

    def bind_proposal(
        self, action: models.FrozenActionProposal
    ) -> models.FrozenActionProposal:
        if not self._bound:
            return action
        try:
            result = self._client.call("action_bind", action)
        except ServiceProtocolError as exc:
            raise ActionDispatcherError(str(exc)) from exc
        if not isinstance(result, models.FrozenActionProposal):
            raise ActionDispatcherError("action owner returned an invalid binding")
        return result

    def validate_pending_action(self, action: models.FrozenActionProposal) -> None:
        if not self._bound:
            return
        try:
            self._client.call("action_validate", action)
        except ServiceProtocolError as exc:
            raise ActionDispatcherError(str(exc)) from exc


class RemoteVaultProposalPreparer(KnowledgeVaultWriteProposalPreparer):
    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def propose(
        self, *, request_id: str, changes: Mapping[str, str]
    ) -> models.FrozenActionProposal:
        try:
            result = self._client.call(
                "propose", request_id=request_id, changes=dict(changes)
            )
        except RemoteServiceError as exc:
            raise ActionDispatcherError(str(exc)) from exc
        if not isinstance(result, models.FrozenActionProposal):
            raise ActionDispatcherError("vault service returned an invalid proposal")
        return result


def find_available_port() -> int:
    """Reserve and release one loopback port for isolated process tests."""

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_until_ready(host: str, port: int, *, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://{host}:{port}/health", timeout=0.2) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            time.sleep(0.02)
    raise TimeoutError("owned service did not become ready")
