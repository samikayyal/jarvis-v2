# ruff: noqa: F401, I001, RUF100 -- split modules retain ticket context.
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from test_support import build_receiver_components

from jarvis_control_plane import (
    DurableMemory,
    InboundMessage,
    InMemoryDurableStateStore,
    MemoryLifecycle,
    MemoryOperation,
    MemorySearchLimitExceeded,
    SignedInboundEvent,
    SQLiteDurableStateStore,
    StateStoreError,
    parse_memory_command,
)

from jarvis_control_plane.control_grammar import MessageKind, parse_control

from jarvis_control_plane.models import MemorySelection

from jarvis_control_plane.sessions import (
    InMemoryWorkingSessionStore,
    ModelAvailability,
    PendingActionState,
    accept_request,
    install_pending_action,
)

NOW = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)


class _UnavailableModelProvider:
    def current(self) -> ModelAvailability:
        raise RuntimeError("model provider is unavailable")


def _set_working_session_state(components, *, pending: bool) -> None:
    store = components.broker.working_sessions
    current = store.load()
    assert current is not None
    accepted = accept_request(
        current,
        now=NOW,
        request_id="ticket22-gate-request",
        originating_message_id="ticket22-gate-message",
    )
    assert accepted.cancellation_token is not None
    updated = accepted.state
    if pending:
        action = PendingActionState.create(
            action_id="ticket22-gate-action",
            session_id=updated.session_id,
            request_id="ticket22-gate-request",
            kind="placeholder",
            summary="A bounded pending action",
            created_at=NOW,
        )
        updated = install_pending_action(updated, action, now=NOW).state
    store.compare_and_set(current, updated)


def _event(components, *, message_id: str, text: str) -> SignedInboundEvent:
    message = InboundMessage(
        event_type="message.received",
        session_id=components.config.session_id,
        event_id=f"event-{message_id}",
        message_id=message_id,
        sender_id="operator.test",
        chat_id="operator.test",
        chat_type="direct",
        message_type="text",
        from_me=False,
        text=text,
    )
    return SignedInboundEvent.from_message(message, components.config.signing_secret)


def test_durable_memory_is_explicitly_created_inspectable_and_persists_across_restart(
    tmp_path,
) -> None:
    database = tmp_path / "state.sqlite3"
    memory = DurableMemory(
        memory_id="memory-001",
        content="The operator prefers concise status updates.",
        created_at=NOW,
        updated_at=NOW,
        source_message_id="message-remember-001",
    )

    state = SQLiteDurableStateStore(database)
    try:
        assert state.list_memories() == ()
        state.create_memory(memory)

        assert state.get_memory("memory-001") == memory
        assert state.list_memories() == (memory,)
    finally:
        state.close()

    reopened = SQLiteDurableStateStore(database)
    try:
        persisted = reopened.get_memory("memory-001")
        assert persisted is not None
        assert persisted.content == memory.content
        assert persisted.status is MemoryLifecycle.ACTIVE
        assert persisted.source_message_id == "message-remember-001"
    finally:
        reopened.close()


def test_memory_parser_requires_explicit_remember_language_and_preserves_content() -> (
    None
):
    assert parse_memory_command("I remember that tea is good") is None
    assert parse_memory_command("Remember when we discussed the deployment?") is None
    assert parse_memory_command("Please remember I prefer tea.") is None
    natural = parse_memory_command("Please remember that I prefer tea.")
    assert natural is not None
    assert natural.operation is MemoryOperation.REMEMBER
    assert natural.source == "natural"
    assert natural.content == "I prefer tea."

    slash = parse_memory_command("/memory replace memory-001 Keep Title Case")
    assert slash is not None
    assert slash.operation is MemoryOperation.REPLACE
    assert slash.memory_id == "memory-001"
    assert slash.content == "Keep Title Case"


def test_memory_parser_is_the_only_authority_for_memory_command_grammar() -> None:
    assert parse_control("/memory list").kind is MessageKind.UNKNOWN_COMMAND
    command = parse_memory_command("/memory list")
    assert command is not None
    assert command.operation is MemoryOperation.LIST


@pytest.mark.parametrize(
    ("text", "operation"),
    (
        ("/memory search", MemoryOperation.SEARCH),
        ("/memory remember", MemoryOperation.REMEMBER),
    ),
)
def test_memory_parser_represents_missing_arguments_without_raising(
    text: str, operation: MemoryOperation
) -> None:
    command = parse_memory_command(text)
    assert command is not None
    assert command.operation is operation
    assert command.content is None
    assert command.is_valid is False
    assert command.error


@pytest.mark.parametrize(
    "text",
    (
        "/memory search",
        "/memory remember",
        "/memory list extra",
        "/memory inspect memory-invalid extra",
        "/memory forget memory-invalid extra",
        "/memory unknown",
    ),
)
def test_invalid_memory_commands_return_usage_instead_of_escaping_receiver(
    text: str,
) -> None:
    slug = text.removeprefix("/memory ").replace(" ", "-")
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id=f"session.ticket22.invalid-{slug}",
        signing_secret=b"ticket22-invalid-secret",
        now=NOW,
        id_prefix="ticket22-invalid",
    )

    result = components.receiver.receive(
        _event(components, message_id=f"message-invalid-{slug}", text=text)
    )

    assert result.disposition == "memory_invalid"
    assert result.reason is None
    assert components.orchestration.calls == []


@pytest.mark.parametrize(
    "memory_command",
    ("/memory list", "/memory search key", "/memory inspect memory-gate"),
)
@pytest.mark.parametrize("pending", (False, True))
def test_memory_reads_respect_active_request_and_pending_action_gates(
    memory_command: str, pending: bool
) -> None:
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id=f"session.ticket22.gate-{pending}-{memory_command.count(' ')}",
        signing_secret=b"ticket22-gate-secret",
        now=NOW,
        id_prefix="ticket22-gate",
        working_sessions=InMemoryWorkingSessionStore(),
    )
    components.state.create_memory(
        DurableMemory(
            memory_id="memory-gate",
            content="The gate test memory.",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    _set_working_session_state(components, pending=pending)

    result = components.receiver.receive(
        _event(
            components,
            message_id=f"message-gate-{pending}-{memory_command.count(' ')}",
            text=memory_command,
        )
    )

    assert result.disposition == ("pending_blocked" if pending else "busy_refused")
    assert components.orchestration.calls == []
    assert all(
        not (
            reply.body.startswith("Durable assistant memory")
            or "memory-gate |" in reply.body
        )
        for reply in components.outbound.sent
    )


def test_explicit_exact_memory_use_allows_credential_like_memory_in_orchestration() -> (
    None
):
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.ticket22.explicit-use",
        signing_secret=b"ticket22-explicit-use-secret",
        now=NOW,
        id_prefix="ticket22-explicit-use",
    )
    components.state.create_memory(
        DurableMemory(
            memory_id="memory-explicit-secret",
            content="The API key is sk-proj-ticket22-explicit-value.",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    components.state.create_memory(
        DurableMemory(
            memory_id="memory-not-selected",
            content="The operator prefers concise updates.",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    result = components.receiver.receive(
        _event(
            components,
            message_id="message-explicit-use",
            text="/memory use memory-explicit-secret What is the exact API key?",
        )
    )

    assert result.disposition == "completed"
    assert len(components.orchestration.calls) == 1
    call = components.orchestration.calls[0]
    assert call.text == "What is the exact API key?"
    assert [memory.memory_id for memory in call.memories] == ["memory-explicit-secret"]
    assert call.memories[0].credential_like is True
    assert "memory-not-selected" not in {memory.memory_id for memory in call.memories}
    assert any(
        "memory-explicit-secret from message none" in reply.body
        for reply in components.outbound.sent
    )


@pytest.mark.parametrize(
    ("availability", "expected_disposition"),
    (
        (
            ModelAvailability(
                available_models=("gpt-5.6-sol",),
                available_reasoning_levels=("medium",),
            ),
            "model_unavailable",
        ),
        (
            ModelAvailability(
                available_models=("gpt-5.6-luna",),
                available_reasoning_levels=("high",),
            ),
            "reasoning_unavailable",
        ),
    ),
)
def test_explicit_memory_use_reuses_model_availability_admission(
    availability: ModelAvailability, expected_disposition: str
) -> None:
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id=f"session.ticket22.availability-{expected_disposition}",
        signing_secret=b"ticket22-availability-secret",
        now=NOW,
        id_prefix=f"ticket22-availability-{expected_disposition}",
        availability=availability,
    )
    components.state.create_memory(
        DurableMemory(
            memory_id="memory-availability",
            content="The API key is sk-proj-ticket22-availability-value.",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    result = components.receiver.receive(
        _event(
            components,
            message_id=f"message-availability-{expected_disposition}",
            text="/memory use memory-availability What is the exact API key?",
        )
    )

    assert result.disposition == expected_disposition
    assert components.orchestration.calls == []
    current = components.broker.working_sessions.load()
    assert current is not None
    assert current.active_request is None
    assert components.state.list_requests() == ()


def test_explicit_memory_use_fails_closed_when_model_availability_provider_fails() -> (
    None
):
    components = build_receiver_components(
        operator_id="operator.test",
        transport_session_id="session.ticket22.availability-provider",
        signing_secret=b"ticket22-provider-secret",
        now=NOW,
        id_prefix="ticket22-provider",
    )
    components.broker.model_availability_provider = _UnavailableModelProvider()
    components.state.create_memory(
        DurableMemory(
            memory_id="memory-provider-failure",
            content="The API key is sk-proj-ticket22-provider-value.",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    result = components.receiver.receive(
        _event(
            components,
            message_id="message-provider-failure",
            text="/memory use memory-provider-failure What is the exact API key?",
        )
    )

    assert result.status_code == 503
    assert result.disposition == "model_availability_unavailable"
    assert components.orchestration.calls == []
    current = components.broker.working_sessions.load()
    assert current is not None
    assert current.active_request is None
    assert components.state.list_requests() == ()


def test_explicit_memory_selection_requires_one_active_record() -> None:
    with pytest.raises(ValueError, match="exactly one active memory"):
        MemorySelection(memories=(), explicit=True)

    first_secret = DurableMemory(
        memory_id="memory-selection-secret-1",
        content="API key: sk-proj-ticket22-selection-one",
        created_at=NOW,
        updated_at=NOW,
    )
    second_secret = DurableMemory(
        memory_id="memory-selection-secret-2",
        content="API key: sk-proj-ticket22-selection-two",
        created_at=NOW,
        updated_at=NOW,
    )
    assert first_secret.credential_like is True
    assert second_secret.credential_like is True

    with pytest.raises(ValueError, match="exactly one active memory"):
        MemorySelection(memories=(first_secret, second_secret), explicit=True)


@pytest.mark.parametrize("store_kind", ("memory", "sqlite"))
def test_memory_lifecycle_contract_covers_both_durable_store_adapters(
    store_kind: str, tmp_path
) -> None:
    state = (
        InMemoryDurableStateStore()
        if store_kind == "memory"
        else SQLiteDurableStateStore(tmp_path / "memory-lifecycle.sqlite3")
    )
    try:
        original = DurableMemory(
            memory_id="memory-contract-original",
            content="The original contract memory.",
            created_at=NOW,
            updated_at=NOW,
        )
        secret = DurableMemory(
            memory_id="memory-contract-secret",
            content="API key: sk-proj-ticket22-contract-value.",
            created_at=NOW,
            updated_at=NOW,
        )
        replacement = DurableMemory(
            memory_id="memory-contract-replacement",
            content="The replacement contract memory.",
            created_at=NOW,
            updated_at=NOW,
        )
        state.create_memory(original)
        state.create_memory(secret)
        assert state.search_memories(memory_ids=(original.memory_id,)) == (original,)
        assert (
            state.select_memories_for_context(text="ticket22", limit=5).memories == ()
        )

        state.replace_memory(
            original.memory_id,
            replacement,
            expected_revision=original.revision_digest,
        )
        retired = state.get_memory(original.memory_id)
        assert retired is not None
        assert retired.is_terminal
        assert retired.content is None
        assert state.search_memories(text="replacement") == (replacement,)
        assert state.select_memories_for_context(
            text="replacement", limit=5
        ).memories == (replacement,)
        assert state.list_memories(include_terminal=False) == (replacement, secret)

        with pytest.raises(StateStoreError):
            state.replace_memory(
                replacement.memory_id,
                DurableMemory(
                    memory_id="memory-contract-stale",
                    content="A stale replacement.",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                expected_revision="stale-revision",
            )
        assert state.get_memory("memory-contract-stale") is None

        state.forget_memory(
            secret.memory_id,
            expected_revision=secret.revision_digest,
            updated_at=NOW,
        )
        forgotten = state.get_memory(secret.memory_id)
        assert forgotten is not None
        assert forgotten.is_terminal
        assert forgotten.content is None
        assert state.select_memories_for_context(text="API key", limit=5).memories == ()
        assert state.list_memories(include_terminal=False) == (replacement,)
    finally:
        if isinstance(state, SQLiteDurableStateStore):
            state.close()


def test_in_memory_and_sqlite_memory_contracts_use_the_same_exact_selector() -> None:
    stores = (InMemoryDurableStateStore(), SQLiteDurableStateStore())
    for state in stores:
        state.create_memory(
            DurableMemory(
                memory_id="memory-002",
                content="A second memory.",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        assert state.search_memories(memory_ids=("memory-002",))
        if isinstance(state, SQLiteDurableStateStore):
            state.close()
