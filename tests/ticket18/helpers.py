from __future__ import annotations

import json
from datetime import (
    UTC,
    datetime,
)

from test_support import build_receiver_components

from jarvis_control_plane import (
    ActionDispatcher,
    ControlledGmailWriteProvider,
    DeterministicIdGenerator,
    DiagnosticTraceRecorder,
    FixedClock,
    FrozenActionProposal,
    GmailWriteConnector,
    GoogleConnectionState,
    InboundMessage,
    InMemoryAuditBoundary,
    InMemoryDiagnosticTraceStore,
    InMemoryGoogleCredentialStore,
    OAuthCredentialRecord,
    SignedInboundEvent,
    create_gmail_new_send_proposal,
    create_gmail_reply_proposal,
)
from jarvis_control_plane.acceptance_failpoints import ReviewedPostDispatchFailpoint

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

IDENTITY = "google-subject-123"

OPERATOR = "operator.test"

TRANSPORT_SESSION = "session.test"

SECRET = b"ticket18-test-secret"


def _event(text: str, *, suffix: str) -> SignedInboundEvent:
    return SignedInboundEvent.from_message(
        InboundMessage(
            event_type="message.received",
            session_id=TRANSPORT_SESSION,
            event_id=f"event-{suffix}",
            message_id=f"message-{suffix}",
            sender_id=OPERATOR,
            chat_id=OPERATOR,
            chat_type="direct",
            message_type="text",
            from_me=False,
            text=text,
        ),
        SECRET,
    )


def _trace() -> DiagnosticTraceRecorder:
    return DiagnosticTraceRecorder(
        writer=InMemoryDiagnosticTraceStore().writer(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-trace"),
    )


def _dispatcher(
    provider: ControlledGmailWriteProvider,
    *,
    audit: InMemoryAuditBoundary | None = None,
    connection_state: object | None = None,
    trace: DiagnosticTraceRecorder | None = None,
    acceptance_failpoint: ReviewedPostDispatchFailpoint | None = None,
) -> GmailWriteConnector:
    connection_state = connection_state or (
        lambda: GoogleConnectionState(
            connected=True,
            generation=1,
            granted_scopes=frozenset({"https://www.googleapis.com/auth/gmail.send"}),
        )
    )
    return GmailWriteConnector(
        configured_identity=IDENTITY,
        credential_store=InMemoryGoogleCredentialStore(
            OAuthCredentialRecord(
                subject=IDENTITY,
                granted_scopes=frozenset(
                    {"https://www.googleapis.com/auth/gmail.send"}
                ),
                refresh_token="controlled-refresh-token",
            )
        ),
        provider=provider,
        audit=audit or InMemoryAuditBoundary(),
        trace=trace or _trace(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("ticket18-gmail"),
        connection_state=connection_state,  # type: ignore[arg-type]
        acceptance_failpoint=acceptance_failpoint,
    )


def _proposal(*, reply: bool = False) -> FrozenActionProposal:
    fields: dict[str, object] = {
        "to": ("recipient@example.com",),
        "cc": ("copy@example.com",),
        "bcc": ("blind@example.com",),
        "subject": "Quarterly check-in",
        "body": "Hello\n\nPlease review the attached plan.",
        "mime_type": "text/plain",
    }
    if reply:
        fields.update(
            {
                "source_message_id": "source-001",
                "source_thread_id": "thread-001",
                "in_reply_to": "<source-001@example.com>",
                "references": ("<root@example.com>", "<source-001@example.com>"),
            }
        )
    factory = create_gmail_reply_proposal if reply else create_gmail_new_send_proposal
    return factory(action_id="gmail-action-001", request_id="request-001", **fields)


def _terminal_proposal() -> FrozenActionProposal:
    return FrozenActionProposal.create(
        action_id="terminal-action-001",
        request_id="request-001",
        kind="terminal",
        preview="Run the exact terminal action.",
        payload={
            "host": "ubuntu",
            "executable": "/usr/bin/touch",
            "arguments": ["/workspace/output.txt"],
            "cwd": "/workspace",
        },
    )


def _components(
    proposal: FrozenActionProposal,
    dispatcher: ActionDispatcher,
    *,
    audit: InMemoryAuditBoundary | None = None,
    trace: DiagnosticTraceRecorder | None = None,
    action_id: str | None = None,
) -> object:
    from jarvis_control_plane import ControlledOrchestrationAdapter

    return build_receiver_components(
        operator_id=OPERATOR,
        transport_session_id=TRANSPORT_SESSION,
        signing_secret=SECRET,
        now=NOW,
        id_prefix="ticket18",
        audit=audit,
        orchestration=ControlledOrchestrationAdapter(
            proposal_factory=lambda request: FrozenActionProposal.create(
                action_id=action_id or f"{request.state.request_id}:gmail",
                request_id=request.state.request_id,
                kind=proposal.kind,
                preview=proposal.preview,
                payload=json.loads(proposal.payload),
            )
        ),
        action_dispatcher=dispatcher,  # type: ignore[arg-type]
        action_lifecycle=dispatcher,  # type: ignore[arg-type]
        trace=trace,
    )
