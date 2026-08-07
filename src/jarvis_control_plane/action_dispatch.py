"""Explicit routing for the broker's approval-gated action surface."""

from __future__ import annotations

from threading import RLock

from .models import FrozenActionProposal
from .ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcher,
    ActionDispatcherError,
    ActionDispatchHandle,
    ActionFinalizer,
    BoundActionLifecycle,
)


class RoutedActionDispatcher:
    """Route each frozen action kind to its owning capability dispatcher."""

    def __init__(
        self,
        *,
        terminal: ActionDispatcher,
        gmail: ActionDispatcher,
        gmail_lifecycle: BoundActionLifecycle,
    ) -> None:
        self._dispatchers = {
            "terminal": terminal,
            "gmail_send": gmail,
            "gmail_reply": gmail,
        }
        self._bound_lifecycles = {
            "gmail_send": gmail_lifecycle,
            "gmail_reply": gmail_lifecycle,
        }
        self._prepared: dict[str, tuple[ActionDispatcher, ActionDispatchHandle]] = {}
        self._lock = RLock()

    def bind_proposal(self, action: FrozenActionProposal) -> FrozenActionProposal:
        """Bind only through a connector that owns external mutable state."""

        lifecycle = self._bound_lifecycle_for(action)
        return action if lifecycle is None else lifecycle.bind_proposal(action)

    def validate_pending_action(self, action: FrozenActionProposal) -> None:
        """Revalidate only through a connector with mutable external state."""

        lifecycle = self._bound_lifecycle_for(action)
        if lifecycle is not None:
            lifecycle.validate_pending_action(action)

    def dispatch(self, action: FrozenActionProposal) -> None:
        """Dispatch only through the connector that owns the action kind.

        This compatibility method keeps the direct connector surface used by
        Ticket 18 callers while the broker uses the prepared/cancellable port.
        """

        self.prepare(action).run()

    def prepare(self, action: FrozenActionProposal) -> ActionDispatchHandle:
        """Prepare one routed action without starting its external operation."""

        dispatcher = self._dispatcher_for(action)
        prepare = getattr(dispatcher, "prepare", None)
        if not callable(prepare):
            raise ActionDispatcherError(
                f"dispatcher for {action.kind!r} does not support prepared dispatch"
            )
        handle = prepare(action)
        if not isinstance(handle, ActionDispatchHandle):
            raise ActionDispatcherError(
                f"dispatcher for {action.kind!r} returned an invalid dispatch handle"
            )
        with self._lock:
            if action.action_id in self._prepared:
                raise ActionDispatcherError(
                    f"action {action.action_id} is already prepared",
                    may_have_dispatched=True,
                )
            self._prepared[action.action_id] = (dispatcher, handle)
        return handle

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        """Cancel one prepared routed action, preserving unknown outcomes."""

        with self._lock:
            prepared = self._prepared.get(action_id)
        if prepared is None:
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        _dispatcher, handle = prepared
        cancel = getattr(handle, "cancel", None)
        if not callable(cancel):
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        try:
            result = cancel()
        except Exception:  # noqa: BLE001 - an unavailable edge is unknown
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        if not isinstance(result, ActionCancellationResult):
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        return result

    def finalize(self, *, action_id: str) -> None:
        """Retire the routed handle after the broker durably closes it."""

        with self._lock:
            prepared = self._prepared.pop(action_id, None)
        if prepared is None:
            return
        dispatcher, _handle = prepared
        if not isinstance(dispatcher, ActionFinalizer):
            return
        try:
            dispatcher.finalize(action_id=action_id)
        except Exception:  # noqa: BLE001 - durable closure is authoritative
            return

    def _dispatcher_for(self, action: FrozenActionProposal) -> ActionDispatcher:
        try:
            return self._dispatchers[action.kind]
        except KeyError as exc:
            raise ActionDispatcherError(
                f"no action dispatcher is configured for {action.kind!r}"
            ) from exc

    def _bound_lifecycle_for(
        self, action: FrozenActionProposal
    ) -> BoundActionLifecycle | None:
        return self._bound_lifecycles.get(action.kind)
