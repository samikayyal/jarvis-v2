"""Gmail action contracts for the Google integration."""

from .actions import (
    GMAIL_SEND_SCOPE,
    GmailMessage,
    GmailNewSendRequest,
    GmailOperation,
    GmailReplyRequest,
    GmailWriteRequest,
    create_gmail_new_send_proposal,
    create_gmail_reply_proposal,
    gmail_proposal_payload,
    gmail_proposal_preview,
    gmail_write_request_from_proposal,
)

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
