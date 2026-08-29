"""Orchestration and OpenWA connector composition roots."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


class RemoteGoogleReads:
    """Expose the Google read connector through the authenticated service link."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def gmail_messages_list(self, **kwargs: object) -> object:
        return self._client.call("gmail_messages_list", **kwargs)

    def gmail_messages_get(self, **kwargs: object) -> object:
        return self._client.call("gmail_messages_get", **kwargs)

    def gmail_threads_list(self, **kwargs: object) -> object:
        return self._client.call("gmail_threads_list", **kwargs)

    def gmail_threads_get(self, **kwargs: object) -> object:
        return self._client.call("gmail_threads_get", **kwargs)

    def drive_files_list(self, **kwargs: object) -> object:
        return self._client.call("drive_files_list", **kwargs)

    def drive_files_get(self, **kwargs: object) -> object:
        return self._client.call("drive_files_get", **kwargs)

    def drive_files_export(self, **kwargs: object) -> object:
        return self._client.call("drive_files_export", **kwargs)

    def current_connection_generation(self) -> int:
        result = self._client.call("current_connection_generation")
        if isinstance(result, bool) or not isinstance(result, int) or result < 0:
            raise self._service_protocol_error(
                "Google service returned invalid connection generation"
            )
        return result

    @staticmethod
    def _service_protocol_error(message: str) -> type[Exception]:
        from .service_protocol import ServiceProtocolError

        return ServiceProtocolError(message)


def orchestration_operations(
    config: Mapping[str, Any], *, runtime: Any
) -> Mapping[str, Callable[..., object]]:
    credential = runtime._credential_json(
        runtime.Path("/run/credentials/openai/credentials.json")
    )
    api_key = runtime._require_text(credential.get("api_key"), "OpenAI api_key")
    from agents import RunConfig
    from agents.models.openai_provider import OpenAIProvider

    model_provider = OpenAIProvider(api_key=api_key)
    google = runtime._RemoteGoogleReads(
        runtime._client(
            config,
            client_identity="jarvis-orchestration",
            server_role="google_connector",
        )
    )
    vault_client = runtime._client(
        config,
        client_identity="jarvis-orchestration",
        server_role="knowledge_vault_connector",
    )
    timeouts = config.get("timeouts")
    if not isinstance(timeouts, Mapping):
        raise runtime.CompositionError(
            "orchestration timeout configuration is incomplete"
        )

    def read_vault(_request: object, typed_input: object, deadline: float) -> object:
        if not isinstance(typed_input, runtime.VaultReadInput):
            raise TypeError("read_knowledge_vault received invalid input")
        payload = vault_client.call(
            "read", typed_input.model_dump(mode="json"), deadline=deadline
        )
        try:
            result = runtime.KnowledgeVaultReadResult.model_validate(payload)
        except (TypeError, ValueError):
            raise TypeError("vault service returned invalid read output")
        return result

    orchestration = runtime.AgentsSdkOrchestrationAdapter(
        google_read_connector=google,
        vault_read_tool=runtime.BoundedReadTool(
            name="read_knowledge_vault",
            description="Read or search the configured knowledge vault locally.",
            input_model=runtime.VaultReadInput,
            output_model=runtime.KnowledgeVaultReadResult,
            handler=read_vault,  # type: ignore[arg-type]
        ),
        vault_write_enabled=True,
        model_turn_timeout_seconds=float(timeouts.get("model_turn_seconds", 0)),
        run_config_factory=lambda **kwargs: RunConfig(
            model_provider=model_provider, **kwargs
        ),
    )
    return {"run": orchestration.run, "cancel": orchestration.cancel}


def openwa_operations(
    config: Mapping[str, Any], *, runtime: Any
) -> Mapping[str, Callable[..., object]]:
    deployment = config.get("deployment")
    if not isinstance(deployment, Mapping):
        raise runtime.CompositionError("deployment configuration is unavailable")
    credential = runtime._credential_json(
        runtime.Path("/run/credentials/openwa/credentials.json")
    )
    connector = runtime.OpenWAOutboundConnector(
        config=runtime.OpenWAConfig(
            api_base_url=runtime._require_text(
                credential.get("api_base_url"), "OpenWA api_base_url"
            ),
            api_key=runtime._require_text(credential.get("api_key"), "OpenWA api_key"),
            internal_session_id=runtime._require_text(
                deployment.get("openwa_internal_session_id"),
                "openwa_internal_session_id",
            ),
            named_session=runtime._require_text(
                deployment.get("openwa_named_session"), "openwa_named_session"
            ),
            operator_conversation_id=runtime._require_text(
                deployment.get("openwa_operator_conversation_id"),
                "openwa_operator_conversation_id",
            ),
        )
    )
    return {
        "current": connector.readiness.current,
        "preflight": connector.preflight,
        "send": connector.send,
    }
