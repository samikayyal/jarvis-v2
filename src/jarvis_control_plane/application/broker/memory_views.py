# ruff: noqa: F401, F403, I001, RUF100 -- workflow mixin uses the shared broker namespace.
"""Broker memory views workflow."""

from __future__ import annotations

from .support import *


class _BrokerMemoryViewsMixin:
    def _remember_memory_proposal(
        self, message: InboundMessage, command: MemoryCommand
    ) -> tuple[str, dict[str, object]]:
        if command.content is None:
            raise ValueError("durable-memory content is required")
        memory_id = self.ids.new_id("memory")
        payload = {
            "operation": MemoryOperation.REMEMBER.value,
            "memory_id": memory_id,
            "content": command.content,
            "source_message_id": message.message_id,
        }
        preview = (
            "Create durable assistant memory\n"
            f"Memory ID: {memory_id}\n"
            f"Exact content: {command.content}\n"
            f"Source message: {message.message_id}"
        )
        return preview, payload

    def _replace_memory_proposal(
        self,
        message: InboundMessage,
        command: MemoryCommand,
        target: DurableMemory | None,
    ) -> tuple[str, dict[str, object]]:
        if target is None or target.content is None or command.content is None:
            raise ValueError(
                "durable-memory replacement target and content are required"
            )
        new_memory_id = self.ids.new_id("memory")
        payload = {
            "operation": MemoryOperation.REPLACE.value,
            "memory_id": target.memory_id,
            "new_memory_id": new_memory_id,
            "content": command.content,
            "expected_revision": target.revision_digest,
            "source_message_id": message.message_id,
        }
        preview = (
            f"Replace durable assistant memory {target.memory_id}\n"
            f"Current exact content: {target.content}\n"
            f"Replacement exact content: {command.content}\n"
            f"New memory ID: {new_memory_id}\n"
            f"Source message: {message.message_id}"
        )
        return preview, payload

    def _forget_memory_proposal(
        self, message: InboundMessage, target: DurableMemory | None
    ) -> tuple[str, dict[str, object]]:
        if target is None or target.content is None:
            raise ValueError("durable-memory forget target is required")
        payload = {
            "operation": MemoryOperation.FORGET.value,
            "memory_id": target.memory_id,
            "expected_revision": target.revision_digest,
            "source_message_id": message.message_id,
        }
        preview = (
            f"Forget durable assistant memory {target.memory_id}\n"
            f"Exact content to remove: {target.content}\n"
            f"Source message: {target.source_message_id or 'none'}"
        )
        return preview, payload

    @staticmethod
    def _render_memory_list(
        memories: tuple[DurableMemory, ...], *, searched: bool = False
    ) -> str:
        if not memories:
            return (
                "No durable assistant memories matched."
                if searched
                else "No durable assistant memories exist."
            )
        heading = (
            "Durable assistant memory matches (inspect by exact ID):"
            if searched
            else "Durable assistant memories (inspect by exact ID):"
        )
        return "\n".join(
            (
                heading,
                *(
                    f"{memory.memory_id} | {memory.status.value} | "
                    f"credential-like={str(memory.credential_like).lower()} | "
                    f"source={memory.source_message_id or 'none'} | "
                    f"updated={memory.updated_at.isoformat()}"
                    for memory in memories
                ),
            )
        )

    @staticmethod
    def _render_memory_inspect(memory: DurableMemory | None) -> str:
        if memory is None:
            return "No durable assistant memory has that exact ID."
        content = (
            memory.content
            if memory.content is not None
            else "[content unavailable: terminal memory record]"
        )
        return (
            f"Durable assistant memory {memory.memory_id}\n"
            f"Status: {memory.status.value}\n"
            f"Credential-like: {str(memory.credential_like).lower()}\n"
            f"Source message: {memory.source_message_id or 'none'}\n"
            f"Created: {memory.created_at.isoformat()}\n"
            f"Updated: {memory.updated_at.isoformat()}\n"
            f"Exact content: {content}"
        )

    def _dispatch_memory_text(
        self,
        message: InboundMessage,
        text: str,
        *,
        disposition: str,
    ) -> ReceiveResult:
        control_id = self.ids.new_id("control")
        result = self._dispatch_control_text(
            message,
            body=_bounded_informational_reply(text, request_id=control_id),
            control_id=control_id,
        )
        if result.disposition != "control_sent":
            return result
        return replace(result, disposition=disposition)

    @staticmethod
    def _render_history_search(messages: tuple[ConversationMessage, ...]) -> str:
        if not messages:
            return "No accessible conversation messages matched."
        return "\n".join(
            (
                "Conversation-history matches (inspect or export by exact history ID):",
                *(
                    f"{item.history_id} | conversation {item.working_session_id} | "
                    f"{item.direction} | {item.occurred_at.isoformat()} | "
                    f"request {item.request_id or 'none'}"
                    for item in messages
                ),
            )
        )

    def _dispatch_history_text(
        self,
        message: InboundMessage,
        text: str,
        *,
        disposition: str,
    ) -> ReceiveResult:
        control_id = self.ids.new_id("control")
        body = _bounded_informational_reply(text, request_id=control_id)
        result = self._dispatch_control_text(message, body=body, control_id=control_id)
        if result.disposition != "control_sent":
            return result
        return replace(result, disposition=disposition)

    def _dispatch_history_export(
        self,
        message: InboundMessage,
        payload: str,
        *,
        disposition: str,
    ) -> ReceiveResult:
        return self._dispatch_exact_text_export(
            message,
            payload,
            label="Conversation-history export",
            disposition=disposition,
        )

    def _dispatch_exact_text_export(
        self,
        message: InboundMessage,
        payload: str,
        *,
        label: str,
        disposition: str,
    ) -> ReceiveResult:
        control_id = self.ids.new_id("control")
        fragments = tuple(
            payload[index : index + _PROPOSAL_FRAGMENT_PAYLOAD_CHARS]
            for index in range(0, len(payload), _PROPOSAL_FRAGMENT_PAYLOAD_CHARS)
        )
        if not fragments:
            raise InvariantViolation("exact export payload must be non-blank")
        result: ReceiveResult | None = None
        for number, fragment in enumerate(fragments, start=1):
            body = (
                f"{label} part {number}/{len(fragments)} "
                f"request_id={control_id}\n{fragment}"
            )
            result = self._dispatch_control_text(
                message, body=body, control_id=control_id
            )
            if result.disposition != "control_sent":
                return result
        assert result is not None
        return replace(result, disposition=disposition)

    def _selected_configuration_is_available(self, request: RequestState) -> bool:
        try:
            return self._model_availability().supports(
                model=request.model,
                reasoning=request.reasoning,
            )
        except (TypeError, ValueError, RuntimeError):
            return False
