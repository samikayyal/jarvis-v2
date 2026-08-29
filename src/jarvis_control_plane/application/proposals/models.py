"""Pydantic and domain models for proposal translation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...models import FrozenActionProposal, OrchestrationProposalIntent

_MAX_REPLY_CHARS = 3_000

ExecutionHost = Literal["ubuntu", "windows"]
HostReasonCode = Literal[
    "default_ubuntu",
    "explicit_windows",
    "windows_dependency",
]


class AgentsSdkProposal(BaseModel):
    """The one model-emittable proposal shape available in this implementation."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "terminal",
        "gmail_send",
        "gmail_reply",
        "knowledge_vault_write",
    ]
    preview: str = Field(min_length=1, max_length=2_000)
    payload: dict[str, object]


class AgentsSdkPlan(BaseModel):
    """Closed structured output with host selection only for terminal work."""

    model_config = ConfigDict(extra="forbid")

    reply_text: str = Field(min_length=1, max_length=_MAX_REPLY_CHARS)
    execution_host: ExecutionHost | None = None
    host_reason_code: HostReasonCode | None = None
    proposal: AgentsSdkProposal | None = None


class _TerminalStructuredComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executable: str = Field(min_length=1)
    arguments: list[str]
    operator_before: Literal["", "|", "&&", "||", ";"] = ""
    redirections: list[str] = Field(default_factory=list)


class _TerminalStructuredPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: ExecutionHost
    executable: str = Field(min_length=1)
    arguments: list[str]
    cwd: str = Field(min_length=1)
    components: list[_TerminalStructuredComponent] = Field(default_factory=list)


class _TerminalStructuredProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["terminal"]
    preview: str = Field(min_length=1, max_length=2_000)
    payload: _TerminalStructuredPayload


class _GmailSendStructuredPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    body: str
    mime_type: Literal["text/plain", "text/html"]


class _GmailReplyStructuredPayload(_GmailSendStructuredPayload):
    source_message_id: str
    source_thread_id: str
    in_reply_to: str
    references: list[str] = Field(min_length=1, max_length=20)


class _GmailSendStructuredProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["gmail_send"]
    preview: str = Field(min_length=1, max_length=2_000)
    payload: _GmailSendStructuredPayload


class _GmailReplyStructuredProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["gmail_reply"]
    preview: str = Field(min_length=1, max_length=2_000)
    payload: _GmailReplyStructuredPayload


class _VaultWriteStructuredProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["knowledge_vault_write"]
    preview: str = Field(min_length=1, max_length=2_000)
    payload: dict[str, object]


class _AgentsSdkStructuredPlan(BaseModel):
    """Closed provider schema for the v1 proposal surface."""

    model_config = ConfigDict(extra="forbid")

    reply_text: str = Field(min_length=1, max_length=_MAX_REPLY_CHARS)
    execution_host: ExecutionHost | None = None
    host_reason_code: HostReasonCode | None = None
    proposal: (
        Annotated[
            _TerminalStructuredProposal
            | _GmailSendStructuredProposal
            | _GmailReplyStructuredProposal
            | _VaultWriteStructuredProposal,
            Field(discriminator="kind"),
        ]
        | None
    ) = None

    @model_validator(mode="before")
    @classmethod
    def use_proposal_preview_for_empty_reply(cls, value: object) -> object:
        if not isinstance(value, Mapping) or value.get("reply_text") != "":
            return value
        proposal = value.get("proposal")
        preview = proposal.get("preview") if isinstance(proposal, Mapping) else None
        if not isinstance(preview, str) or not preview:
            return value
        normalized = dict(value)
        normalized["reply_text"] = preview
        return normalized


@dataclass(frozen=True, slots=True)
class PlanTranslation:
    """The complete non-authoritative result of one plan translation."""

    reply_text: str
    proposal: FrozenActionProposal | None = None
    proposal_intent: OrchestrationProposalIntent | None = None
    execution_host: ExecutionHost | None = None
    host_reason_code: HostReasonCode | None = None


__all__ = [
    "AgentsSdkPlan",
    "AgentsSdkProposal",
    "ExecutionHost",
    "HostReasonCode",
    "PlanTranslation",
]
