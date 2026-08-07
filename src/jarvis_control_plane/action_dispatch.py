"""Explicit routing for the broker's approval-gated action surface."""

from __future__ import annotations

from .models import FrozenActionProposal
from .ports import ActionDispatcher, ActionDispatcherError


class RoutedActionDispatcher:
    """Route each frozen action kind to its owning capability dispatcher."""

    def __init__(
        self,
        *,
        terminal: ActionDispatcher,
        gmail: ActionDispatcher,
    ) -> None:
        self._dispatchers = {
            "terminal": terminal,
            "gmail_send": gmail,
            "gmail_reply": gmail,
        }

    def bind_proposal(self, action: FrozenActionProposal) -> FrozenActionProposal:
        """Bind only through the connector that owns the action kind."""

        return self._dispatcher_for(action).bind_proposal(action)

    def validate_pending_action(self, action: FrozenActionProposal) -> None:
        """Revalidate only through the connector that owns the action kind."""

        self._dispatcher_for(action).validate_pending_action(action)

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
