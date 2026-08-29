"""Controlled orchestration, action, and outbound adapters."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace

from ..models import (
    FrozenActionProposal,
    OrchestrationRequest,
    OrchestrationResult,
    OutboundDelivery,
    OutboundReply,
    RequestState,
)
from ..ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcherError,
    AuditBoundary,
    Clock,
    IdGenerator,
    OrchestrationAdapterError,
    OutboundConnectorError,
)
from ..worker_gateway import WorkerExecutionResult


class ControlledOrchestrationAdapter:
    """Deterministic orchestration fake; it cannot authorize or send anything."""

    def __init__(
        self,
        *,
        response_text: str = "Controlled orchestration completed the request.",
        failure: str | None = None,
        response_factory: Callable[[OrchestrationRequest], str] | None = None,
        proposal_factory: Callable[[OrchestrationRequest], FrozenActionProposal]
        | None = None,
    ) -> None:
        if not response_text.strip():
            raise ValueError("response_text must be non-blank")
        self.response_text = response_text
        self.failure = failure
        self.response_factory = response_factory
        self.proposal_factory = proposal_factory
        self.calls: list[OrchestrationRequest] = []

    def run(self, request: OrchestrationRequest) -> OrchestrationResult:
        self.calls.append(request)
        if self.failure is not None:
            raise OrchestrationAdapterError(self.failure)
        reply_text = (
            self.response_factory(request)
            if self.response_factory is not None
            else self.response_text
        )
        return OrchestrationResult(
            request_id=request.state.request_id,
            outcome="completed",
            reply_text=reply_text,
            adapter="controlled",
            proposal=(
                self.proposal_factory(request)
                if self.proposal_factory is not None
                else None
            ),
        )


class _ControlledActionDispatch:
    """Prepared controlled action with the same cancellation barrier as workers."""

    def __init__(
        self, owner: ControlledActionDispatcher, action: FrozenActionProposal
    ) -> None:
        self._owner = owner
        self._action = action
        self._lock = threading.RLock()
        self._started = False
        self._cancelled = False

    def run(self) -> object | None:
        with self._lock:
            if self._cancelled:
                self._owner._forget(self._action.action_id, self)
                raise ActionDispatcherError("action was cancelled before dispatch")
            self._started = True
        try:
            return self._owner._dispatch(self._action)
        finally:
            self._owner._forget(self._action.action_id, self)

    def cancel(self) -> ActionCancellationResult:
        with self._lock:
            if not self._started:
                self._cancelled = True
                result = ActionCancellationResult(ActionCancellationStatus.NOT_STARTED)
                self._owner._forget(self._action.action_id, self)
                return result
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)


class ControlledActionDispatcher:
    """Controlled action edge with an explicit prepared/cancellable lifecycle."""

    def __init__(
        self, *, failure: str | None = None, failure_may_have_dispatched: bool = False
    ) -> None:
        self.failure = failure
        self.failure_may_have_dispatched = failure_may_have_dispatched
        self.dispatched: list[FrozenActionProposal] = []
        self._lock = threading.RLock()
        self._prepared: dict[str, _ControlledActionDispatch] = {}

    def prepare(self, action: FrozenActionProposal) -> _ControlledActionDispatch:
        handle = _ControlledActionDispatch(self, action)
        with self._lock:
            if action.action_id in self._prepared:
                raise ActionDispatcherError(
                    f"action {action.action_id} is already prepared",
                    may_have_dispatched=True,
                )
            self._prepared[action.action_id] = handle
        return handle

    def dispatch(self, action: FrozenActionProposal) -> object | None:
        """Compatibility helper for direct controlled-adapter callers."""

        return self.prepare(action).run()

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        with self._lock:
            handle = self._prepared.get(action_id)
        if handle is None:
            # The prepared handle may have forgotten itself after the external
            # operation returned but before the control plane persisted its
            # terminal state. Absence is therefore not proof of non-start.
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        return handle.cancel()

    def _dispatch(self, action: FrozenActionProposal) -> object | None:
        if self.failure is not None:
            raise ActionDispatcherError(
                self.failure, may_have_dispatched=self.failure_may_have_dispatched
            )
        self.dispatched.append(action)
        if action.kind == "terminal":
            return WorkerExecutionResult.completed()
        return None

    def _forget(self, action_id: str, handle: _ControlledActionDispatch) -> None:
        with self._lock:
            if self._prepared.get(action_id) is handle:
                del self._prepared[action_id]


class ControlledOutboundConnector:
    """Closed fake connector with a fixed destination.

    The capability broker owns the audit admission gate immediately before
    calling this connector.  The retained audit/clock/ID constructor inputs
    keep the ticket01 controlled-adapter shape source-compatible.
    """

    def __init__(
        self,
        *,
        operator_id: str,
        session_id: str,
        audit: AuditBoundary,
        clock: Clock,
        ids: IdGenerator,
        failure: str | None = None,
    ) -> None:
        self.operator_id = operator_id
        self.session_id = session_id
        self.audit = audit
        self.clock = clock
        self.ids = ids
        self.failure = failure
        self.sent: list[OutboundReply] = []

    def send(self, reply: OutboundReply) -> OutboundDelivery:
        self.preflight(reply)
        if self.failure is not None:
            raise OutboundConnectorError(self.failure)

        self.sent.append(reply)
        return OutboundDelivery(outbound_id=self.ids.new_id("outbound"), accepted=True)

    def preflight(self, reply: OutboundReply) -> None:
        """Validate the deterministic send without performing it.

        The broker uses this contract to append the complete outbound audit
        admission before calling ``send``.  A connector whose send can fail
        after preflight must expose that uncertainty instead of implementing
        this method as a best-effort check.
        """

        if reply.session_id != self.session_id:
            raise OutboundConnectorError("reply session is not configured")
        if reply.recipient_id != self.operator_id:
            raise OutboundConnectorError("reply recipient is not configured")
        if reply.request_id not in reply.body:
            raise OutboundConnectorError("reply is missing request correlation")
        if self.failure is not None:
            raise OutboundConnectorError(self.failure)


def replace_request(request: RequestState, **changes: object) -> RequestState:
    """Typed helper kept in the adapter module for small state transitions."""

    return replace(request, **changes)
