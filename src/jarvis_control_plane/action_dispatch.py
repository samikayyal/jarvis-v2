"""Explicit routing for the broker's approval-gated action surface."""

from __future__ import annotations

from .models import FrozenActionProposal
from .ports import (
    ActionDispatcher,
    ActionDispatcherError,
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
        """Dispatch only through the connector that owns the action kind."""

        self._dispatcher_for(action).dispatch(action)

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
