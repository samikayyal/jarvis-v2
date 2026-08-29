"""Compatibility facade for the Gmail action contracts."""

from __future__ import annotations

# Preserve the former module's public and private import surface while the
# implementation lives under the focused Google Gmail integration package.
# ruff: noqa: F401
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from functools import partial
from typing import ClassVar, Literal

_DEFERRED_ACTION_NAMES = frozenset(
    {"create_gmail_new_send_proposal", "create_gmail_reply_proposal"}
)


def _deferred_action(name: str, *args: object, **kwargs: object) -> object:
    from .integrations.google.gmail import actions

    return getattr(actions, name)(*args, **kwargs)


def __getattr__(name: str) -> object:
    if name in _DEFERRED_ACTION_NAMES:
        return partial(_deferred_action, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from .integrations.google.gmail.actions import (
    _IDENTIFIER,
    _MAILBOX,
    _MIME_TYPES,
    GMAIL_SEND_SCOPE,
    GmailMessage,
    GmailNewSendRequest,
    GmailOperation,
    GmailReplyRequest,
    GmailWriteRequest,
    _binding_from_payload,
    _body,
    _canonical_string,
    _common_fields,
    _connection_generation,
    _identifier,
    _message_from_payload,
    _message_id,
    _message_ids,
    _mime_type,
    _new_send_request_from_payload,
    _proposal,
    _recipients,
    _reply_request_from_payload,
    _subject,
    _threading,
    _validate_binding,
    create_gmail_new_send_proposal,
    create_gmail_reply_proposal,
    gmail_proposal_payload,
    gmail_proposal_preview,
    gmail_write_request_from_proposal,
)
from .models import FrozenActionProposal

__all__ = [
    "GMAIL_SEND_SCOPE",
    "GmailMessage",
    "GmailNewSendRequest",
    "GmailOperation",
    "GmailReplyRequest",
    "GmailWriteRequest",
    "create_gmail_new_send_proposal",
    "create_gmail_reply_proposal",
    "gmail_proposal_payload",
    "gmail_proposal_preview",
    "gmail_write_request_from_proposal",
]
