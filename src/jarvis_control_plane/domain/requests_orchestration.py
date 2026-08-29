"""Request lifecycle, orchestration contracts, proposals, and typed replies."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .conversations import ConversationMessage
from .ingress_messaging import _non_empty_identifier, ensure_utc
from .memory import DurableMemory, MemorySelection


@dataclass(frozen=True, slots=True)
class RequestState:
    """Durable, bounded lifecycle state for one admitted request."""

    request_id: str
    event_id: str
    message_id: str
    operator_id: str
    session_id: str
    chat_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    phase: str
    model: str = "gpt-5.6-terra"
    reasoning: str = "medium"
    reply_id: str | None = None
    outcome: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "request_id",
            "event_id",
            "message_id",
            "operator_id",
            "session_id",
            "chat_id",
            "status",
            "phase",
        ):
            _non_empty_identifier(getattr(self, name), name)
        _non_empty_identifier(self.model, "model")
        _non_empty_identifier(self.reasoning, "reasoning")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))

    @property
    def correlation_id(self) -> str:
        """Alias used when describing the request/reply correlation contract."""

        return self.request_id


@dataclass(frozen=True, slots=True)
class OrchestrationRequest:
    """Ephemeral input passed from the broker to an orchestration adapter."""

    state: RequestState
    text: str
    history: tuple[ConversationMessage, ...] = ()
    memories: tuple[DurableMemory, ...] = ()
    memory_selection: MemorySelection | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("orchestration text must be non-blank")
        if any(message.credential_like for message in self.history):
            raise ValueError("orchestration history cannot contain credentials")
        memories = tuple(self.memories)
        if any(not isinstance(memory, DurableMemory) for memory in memories):
            raise TypeError("orchestration memories must contain DurableMemory values")
        if any(not memory.is_active for memory in memories):
            raise ValueError("orchestration memories must be active")
        selection = self.memory_selection
        if selection is not None:
            if not isinstance(selection, MemorySelection):
                raise TypeError("memory_selection must be a MemorySelection")
            if selection.memories != memories:
                raise ValueError(
                    "orchestration memory selection must match its memories"
                )
        elif any(memory.credential_like for memory in memories):
            raise ValueError("orchestration memories cannot contain credentials")
        object.__setattr__(self, "memories", memories)

    @property
    def model(self) -> str:
        """The immutable model snapshot that the adapter must execute."""

        return self.state.model

    @property
    def reasoning(self) -> str:
        """The immutable reasoning snapshot that the adapter must execute."""

        return self.state.reasoning


def _canonical_action_payload(payload: object) -> str:
    """Make a proposal payload immutable before it crosses a trust boundary."""

    if isinstance(payload, str):
        if not payload:
            raise ValueError("action payload must be non-blank")
        return payload
    try:
        frozen = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("action payload must be JSON-serializable") from exc
    if not frozen or frozen == "null":
        raise ValueError("action payload must be non-blank")
    return frozen


def _action_digest(
    *, action_id: str, request_id: str, kind: str, preview: str, payload: str
) -> str:
    material = f"{action_id}\x1f{request_id}\x1f{kind}\x1f{preview}\x1f{payload}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenActionProposal:
    """An immutable, non-authoritative proposal awaiting broker approval."""

    action_id: str
    request_id: str
    kind: str
    preview: str
    payload: str
    digest: str

    def __post_init__(self) -> None:
        for name in ("action_id", "request_id", "kind", "preview", "payload"):
            _non_empty_identifier(getattr(self, name), name)
        expected = _action_digest(
            action_id=self.action_id,
            request_id=self.request_id,
            kind=self.kind,
            preview=self.preview,
            payload=self.payload,
        )
        if self.digest != expected:
            raise ValueError("action digest does not match the frozen proposal")

    @classmethod
    def create(
        cls,
        *,
        action_id: str,
        request_id: str,
        kind: str,
        preview: str,
        payload: object,
    ) -> FrozenActionProposal:
        frozen_payload = _canonical_action_payload(payload)
        return cls(
            action_id=action_id,
            request_id=request_id,
            kind=kind,
            preview=preview,
            payload=frozen_payload,
            digest=_action_digest(
                action_id=action_id,
                request_id=request_id,
                kind=kind,
                preview=preview,
                payload=frozen_payload,
            ),
        )


@dataclass(frozen=True, slots=True)
class OrchestrationMilestone:
    """One bounded, non-authoritative progress update for an active request."""

    stage: str
    message: str

    def __post_init__(self) -> None:
        _non_empty_identifier(self.stage, "milestone stage")
        if len(self.stage) > 64:
            raise ValueError("milestone stage is too long")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("milestone message must be non-blank")
        if len(self.message) > 512:
            raise ValueError("milestone message is too long")


@dataclass(frozen=True, slots=True)
class OrchestrationProposalIntent:
    """Non-authoritative action intent awaiting broker-side preparation."""

    request_id: str
    kind: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        _non_empty_identifier(self.request_id, "request_id")
        _non_empty_identifier(self.kind, "kind")
        if not isinstance(self.payload, Mapping):
            raise TypeError("orchestration proposal intent payload must be a mapping")
        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    """Typed, non-authoritative result returned by orchestration."""

    request_id: str
    outcome: str
    reply_text: str
    adapter: str = "controlled"
    proposal: FrozenActionProposal | None = None
    proposal_intent: OrchestrationProposalIntent | None = None
    execution_host: str | None = None
    host_reason_code: str | None = None
    milestones: tuple[OrchestrationMilestone, ...] = ()

    def __post_init__(self) -> None:
        _non_empty_identifier(self.request_id, "request_id")
        _non_empty_identifier(self.outcome, "outcome")
        if not isinstance(self.reply_text, str) or not self.reply_text.strip():
            raise ValueError("reply_text must be non-blank")
        _non_empty_identifier(self.adapter, "adapter")
        milestones = tuple(self.milestones)
        if len(milestones) > 8:
            raise ValueError("orchestration result has too many milestones")
        if any(
            not isinstance(milestone, OrchestrationMilestone)
            for milestone in milestones
        ):
            raise TypeError("milestones must contain OrchestrationMilestone values")
        object.__setattr__(self, "milestones", milestones)
        if self.proposal is not None and self.proposal.request_id != self.request_id:
            raise ValueError(
                "action proposal request_id must match orchestration result"
            )
        if (
            self.proposal_intent is not None
            and self.proposal_intent.request_id != self.request_id
        ):
            raise ValueError(
                "action proposal intent request_id must match orchestration result"
            )
        if self.proposal is not None and self.proposal_intent is not None:
            raise ValueError(
                "orchestration result cannot contain both a proposal and an intent"
            )
        if self.execution_host is None:
            if self.host_reason_code is not None:
                raise ValueError(
                    "host_reason_code requires an execution_host selection"
                )
        else:
            if self.execution_host not in {"ubuntu", "windows"}:
                raise ValueError("execution_host must be ubuntu or windows")
            if self.host_reason_code not in {
                "default_ubuntu",
                "explicit_windows",
                "windows_dependency",
            }:
                raise ValueError("host_reason_code is not a controlled selection")
            if (
                self.execution_host == "ubuntu"
                and self.host_reason_code != "default_ubuntu"
            ):
                raise ValueError("Ubuntu requires the default_ubuntu host reason")
            if (
                self.execution_host == "windows"
                and self.host_reason_code == "default_ubuntu"
            ):
                raise ValueError("Windows requires an explicit or dependency reason")


@dataclass(frozen=True, slots=True)
class OutboundReply:
    """Typed reply constrained to the admitted operator conversation."""

    reply_id: str
    request_id: str
    session_id: str
    recipient_id: str
    body: str
    quoted_message_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("reply_id", "request_id", "session_id", "recipient_id"):
            _non_empty_identifier(getattr(self, name), name)
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("reply body must be non-blank")
        if self.quoted_message_id is not None:
            _non_empty_identifier(self.quoted_message_id, "quoted_message_id")

    @property
    def correlation_id(self) -> str:
        return self.request_id


@dataclass(frozen=True, slots=True)
class OutboundDelivery:
    """One gateway-confirmed outbound message identity."""

    outbound_id: str
    accepted: bool

    def __post_init__(self) -> None:
        _non_empty_identifier(self.outbound_id, "outbound_id")
        if self.accepted is not True:
            raise ValueError("a recorded outbound delivery must be accepted")


@dataclass(frozen=True, slots=True)
class HistorySelection:
    """Bounded safe history passed to orchestration with its required disclosure."""

    messages: tuple[ConversationMessage, ...]

    def __post_init__(self) -> None:
        if any(message.credential_like for message in self.messages):
            raise ValueError("automatic history selection cannot include credentials")

    @property
    def provenance_disclosure(self) -> str | None:
        if not self.messages:
            return None
        pointers = "; ".join(
            "conversation "
            f"{message.working_session_id}, message {message.message_id} at "
            f"{message.occurred_at.isoformat()}"
            for message in self.messages
        )
        return f"History used: {pointers}."


@dataclass(frozen=True, slots=True)
class ReceiveResult:
    """Externally visible result of one receiver invocation."""

    status_code: int
    disposition: str
    request: RequestState | None = None
    reply: OutboundReply | None = None
    reason: str | None = None

    @property
    def request_id(self) -> str | None:
        return self.request.request_id if self.request else None
