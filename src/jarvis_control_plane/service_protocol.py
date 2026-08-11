"""Authenticated, bounded protocol for owned Jarvis service seams.

The protocol deliberately exposes named operations instead of remote object or
module access.  Both peers authenticate every frame and the server keeps a
short replay window.  The typed codec accepts only the closed Jarvis model
registry; it never imports a type named by an incoming frame.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import importlib
import json
import math
import secrets
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock
from types import MappingProxyType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import models, ports
from .models import AuditEvidence, AuditFilter
from .ports import (
    ActionCancellationResult,
    ActionCancellationStatus,
    ActionDispatcher,
    ActionDispatcherError,
    ActionDispatchHandle,
    ActionFinalizer,
    AuditBoundary,
    AuditWriteError,
    BoundActionLifecycle,
    KnowledgeVaultWriteProposalPreparer,
    MessagingGatewayReadiness,
    MessagingGatewayReadinessProvider,
    OrchestrationAdapter,
    OrchestrationAdapterError,
    OutboundConnector,
    OutboundConnectorError,
)

MAX_REQUEST_FRAME_BYTES = 1_048_576
# Two valid 1 MiB terminal streams can expand six-fold when JSON escapes control
# characters. Leave bounded room for progress events and the authenticated envelope.
MAX_FRAME_BYTES = 16_777_216
MAX_CLOCK_SKEW_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 30.0


class ServiceProtocolError(RuntimeError):
    """A bounded service request or response violated the protocol."""


class ServiceAuthenticationError(ServiceProtocolError):
    """A peer could not authenticate a protocol frame."""


class RemoteServiceError(ServiceProtocolError):
    """An allowlisted operation failed in the owning service."""

    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        may_have_dispatched: bool = False,
        may_have_sent: bool = False,
        operation_started: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.may_have_dispatched = may_have_dispatched
        self.may_have_sent = may_have_sent
        self.operation_started = operation_started


def _type_registry() -> Mapping[str, type[Any]]:
    registry: dict[str, type[Any]] = {}
    approved_modules = (models, ports) + tuple(
        importlib.import_module(f"{__package__}.{name}")
        for name in (
            "google_calendar",
            "google_oauth",
            "google_reads",
            "knowledge_vault",
            "knowledge_vault_writes",
            "openwa",
            "sessions",
            "terminal_policy",
            "worker_gateway",
        )
    )
    for module in approved_modules:
        for value in vars(module).values():
            if isinstance(value, type) and (
                dataclasses.is_dataclass(value) or issubclass(value, Enum)
            ):
                registry[f"{value.__module__}.{value.__qualname__}"] = value
    return MappingProxyType(registry)


_TYPES = _type_registry()


def _encode(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("service protocol floats must be finite")
        return value
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, Enum):
        return {
            "$enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _encode(value.value),
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        type_name = f"{type(value).__module__}.{type(value).__qualname__}"
        if type_name not in _TYPES:
            raise TypeError(f"type is outside the service protocol: {type_name}")
        return {
            "$type": type_name,
            "fields": {
                field.name: _encode(getattr(value, field.name))
                for field in dataclasses.fields(value)
            },
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("service protocol mappings require string keys")
        return {"$mapping": {key: _encode(item) for key, item in value.items()}}
    if isinstance(value, (tuple, list)):
        return {"$sequence": [_encode(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        encoded = [_encode(item) for item in value]
        return {"$set": sorted(encoded, key=lambda item: repr(item))}
    raise TypeError(f"value is outside the service protocol: {type(value).__name__}")


def _decode(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ServiceProtocolError("service protocol floats must be finite")
        return value
    if not isinstance(value, dict):
        raise ServiceProtocolError("invalid typed protocol value")
    if "$enum" in value:
        if set(value) != {"$enum", "value"}:
            raise ServiceProtocolError("invalid encoded enum")
        enum_type = _TYPES.get(value["$enum"])
        if enum_type is None or not issubclass(enum_type, Enum):
            raise ServiceProtocolError("encoded enum is outside the registry")
        return enum_type(_decode(value["value"]))
    if "$type" in value:
        if set(value) != {"$type", "fields"}:
            raise ServiceProtocolError("invalid encoded model")
    elif len(value) != 1:
        raise ServiceProtocolError("invalid typed protocol value")
    if "$bytes" in value:
        try:
            return base64.b64decode(value["$bytes"], validate=True)
        except (TypeError, ValueError) as exc:
            raise ServiceProtocolError("invalid encoded bytes") from exc
    if "$datetime" in value:
        try:
            return datetime.fromisoformat(value["$datetime"])
        except (TypeError, ValueError) as exc:
            raise ServiceProtocolError("invalid encoded datetime") from exc
    if "$decimal" in value:
        try:
            return Decimal(value["$decimal"])
        except (TypeError, InvalidOperation) as exc:
            raise ServiceProtocolError("invalid encoded decimal") from exc
    if "$sequence" in value:
        items = value["$sequence"]
        if not isinstance(items, list):
            raise ServiceProtocolError("invalid encoded sequence")
        return tuple(_decode(item) for item in items)
    if "$set" in value:
        items = value["$set"]
        if not isinstance(items, list):
            raise ServiceProtocolError("invalid encoded set")
        return frozenset(_decode(item) for item in items)
    if "$mapping" in value:
        items = value["$mapping"]
        if not isinstance(items, dict) or not all(isinstance(k, str) for k in items):
            raise ServiceProtocolError("invalid encoded mapping")
        return {key: _decode(item) for key, item in items.items()}
    type_name = value.get("$type")
    fields = value.get("fields")
    model_type = _TYPES.get(type_name)
    if model_type is None or not dataclasses.is_dataclass(model_type):
        raise ServiceProtocolError("encoded type is outside the registry")
    if not isinstance(fields, dict):
        raise ServiceProtocolError("invalid encoded model fields")
    allowed_fields = {field.name for field in dataclasses.fields(model_type)}
    if set(fields) != allowed_fields:
        raise ServiceProtocolError("encoded model fields do not match the registry")
    return model_type(**{name: _decode(item) for name, item in fields.items()})


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sign(frame: Mapping[str, object], secret: bytes) -> str:
    return hmac.new(secret, _canonical_json(frame), hashlib.sha256).hexdigest()


def _signed_frame(
    frame: dict[str, object], secret: bytes, *, max_bytes: int = MAX_FRAME_BYTES
) -> bytes:
    signed = {**frame, "signature": _sign(frame, secret)}
    payload = _canonical_json(signed)
    if len(payload) > max_bytes:
        raise ServiceProtocolError("service protocol frame exceeds its fixed bound")
    return payload


def _verify_frame(
    payload: bytes, secret: bytes, *, max_bytes: int = MAX_FRAME_BYTES
) -> dict[str, object]:
    if len(payload) > max_bytes:
        raise ServiceProtocolError("service protocol frame exceeds its fixed bound")
    try:
        frame = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceProtocolError("service protocol frame is not valid JSON") from exc
    if not isinstance(frame, dict):
        raise ServiceProtocolError("service protocol frame must be an object")
    signature = frame.pop("signature", None)
    if not isinstance(signature, str) or not hmac.compare_digest(
        signature, _sign(frame, secret)
    ):
        raise ServiceAuthenticationError("service protocol authentication failed")
    return frame


def _peek_client_identity(payload: bytes) -> str:
    """Select a per-link key without treating the unverified identity as trusted."""

    if len(payload) > MAX_REQUEST_FRAME_BYTES:
        raise ServiceProtocolError("service protocol frame exceeds its fixed bound")
    try:
        frame = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceProtocolError("service protocol frame is not valid JSON") from exc
    identity = frame.get("client_identity") if isinstance(frame, dict) else None
    if not isinstance(identity, str) or not identity:
        raise ServiceAuthenticationError("service client identity is invalid")
    return identity


class AuthenticatedServiceClient:
    """Synchronous production adapter for one private owned service."""

    def __init__(
        self,
        *,
        identity: str,
        expected_server_identity: str,
        secret: bytes,
        host: str,
        port: int,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        operation_timeouts: Mapping[str, float] | None = None,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("service protocol secret must contain at least 32 bytes")
        if not identity or identity.strip() != identity:
            raise ValueError("service client identity must be canonical")
        if (
            not expected_server_identity
            or expected_server_identity.strip() != expected_server_identity
        ):
            raise ValueError("expected service identity must be canonical")
        self._identity = identity
        self._expected_server_identity = expected_server_identity
        self._secret = secret
        self._url = f"http://{host}:{port}/call"
        self._timeout_seconds = timeout_seconds
        self._operation_timeouts = dict(operation_timeouts or {})
        if any(
            not name
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
            for name, timeout in self._operation_timeouts.items()
        ):
            raise ValueError("service operation timeouts must be positive")

    def call(self, operation: str, *args: object, **kwargs: object) -> object:
        if not operation or operation.strip() != operation or operation.startswith("_"):
            raise ValueError("service operation must be canonical")
        request_id = secrets.token_hex(16)
        frame = {
            "version": 1,
            "client_identity": self._identity,
            "server_identity": self._expected_server_identity,
            "request_id": request_id,
            "issued_at": int(time.time()),
            "operation": operation,
            "arguments": _encode(tuple(args)),
            "keywords": _encode(kwargs),
        }
        request = Request(
            self._url,
            data=_signed_frame(frame, self._secret, max_bytes=MAX_REQUEST_FRAME_BYTES),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            timeout_seconds = self._operation_timeouts.get(
                operation, self._timeout_seconds
            )
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read(MAX_FRAME_BYTES + 1)
        except HTTPError as exc:
            payload = exc.read(MAX_FRAME_BYTES + 1)
        except (TimeoutError, URLError, OSError) as exc:
            raise ServiceProtocolError("owned service is unavailable") from exc
        response_frame = _verify_frame(payload, self._secret)
        if (
            response_frame.get("server_identity") != self._expected_server_identity
            or response_frame.get("request_id") != request_id
        ):
            raise ServiceAuthenticationError("service response identity did not match")
        if response_frame.get("ok") is not True:
            error_type = response_frame.get("error_type")
            message = response_frame.get("message")
            if error_type == "PermissionError" and isinstance(message, str):
                raise PermissionError(message)
            raise RemoteServiceError(
                error_type if isinstance(error_type, str) else "RemoteServiceError",
                message if isinstance(message, str) else "owned service failed",
                may_have_dispatched=response_frame.get("may_have_dispatched") is True,
                may_have_sent=response_frame.get("may_have_sent") is True,
                operation_started=response_frame.get("operation_started") is True,
            )
        return _decode(response_frame.get("result"))


class AuthenticatedServiceServer:
    """Private operation host with fixed peer identity and replay checks."""

    def __init__(
        self,
        *,
        identity: str,
        secret: bytes | None = None,
        client_secrets: Mapping[str, bytes] | None = None,
        host: str,
        port: int,
        operations: Mapping[str, Callable[..., object]],
        allowed_client_identities: Sequence[str] = ("jarvis-broker",),
        allowed_operations_by_client: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        if (secret is None) == (client_secrets is None):
            raise ValueError("provide either one test secret or per-client secrets")
        if not identity or identity.strip() != identity:
            raise ValueError("service server identity must be canonical")
        if any(not item or item.strip() != item for item in allowed_client_identities):
            raise ValueError("allowed service client identities must be canonical")
        selected_secrets = (
            {identity: secret for identity in allowed_client_identities}
            if secret is not None
            else dict(client_secrets or {})
        )
        if set(selected_secrets) != set(allowed_client_identities) or any(
            not isinstance(value, bytes) or len(value) < 32
            for value in selected_secrets.values()
        ):
            raise ValueError("each allowed service client requires a 32-byte secret")
        if not operations or any(
            not name or name.startswith("_") for name in operations
        ):
            raise ValueError("service operations must be a non-empty closed set")
        self.identity = identity
        self.client_secrets = selected_secrets
        self.operations = dict(operations)
        self.allowed_client_identities = frozenset(allowed_client_identities)
        selected_operation_allowlists = (
            {
                identity: frozenset(self.operations)
                for identity in allowed_client_identities
            }
            if allowed_operations_by_client is None
            else {
                identity: frozenset(names)
                for identity, names in allowed_operations_by_client.items()
            }
        )
        if set(selected_operation_allowlists) != set(allowed_client_identities) or any(
            not names or not names <= set(self.operations)
            for names in selected_operation_allowlists.values()
        ):
            raise ValueError(
                "each service client requires a closed operation allowlist"
            )
        self.allowed_operations_by_client = selected_operation_allowlists
        self._seen_requests: dict[str, int] = {}
        self._admission_lock = RLock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "JarvisOwnedService/1"

            def do_GET(self) -> None:
                if self.path != "/health":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def do_POST(self) -> None:
                outer._handle(self)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def shutdown(self) -> None:
        self._server.shutdown()

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        request_id = "unknown"
        response_secret: bytes | None = None
        try:
            if handler.path != "/call":
                raise PermissionError("operation is not allowed")
            if handler.headers.get_content_type() != "application/json":
                raise ServiceProtocolError("service protocol content type is invalid")
            length = int(handler.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_FRAME_BYTES:
                raise ServiceProtocolError("invalid service protocol frame length")
            payload = handler.rfile.read(length)
            client_identity = _peek_client_identity(payload)
            response_secret = self.client_secrets.get(client_identity)
            if response_secret is None:
                raise ServiceAuthenticationError(
                    "service client identity is not allowed"
                )
            frame = _verify_frame(
                payload, response_secret, max_bytes=MAX_REQUEST_FRAME_BYTES
            )
            request_id = str(frame.get("request_id", "unknown"))
            self._admit(frame)
            operation = frame.get("operation")
            client_identity = frame.get("client_identity")
            allowed_operations = self.allowed_operations_by_client.get(
                client_identity if isinstance(client_identity, str) else ""
            )
            if not isinstance(operation, str) or operation not in (
                allowed_operations or frozenset()
            ):
                raise PermissionError("operation is not allowed for this client")
            target = (
                self.operations.get(operation) if isinstance(operation, str) else None
            )
            if target is None:
                raise PermissionError("operation is not allowed")
            arguments = _decode(frame.get("arguments"))
            keywords = _decode(frame.get("keywords"))
            if not isinstance(arguments, tuple) or not isinstance(keywords, dict):
                raise ServiceProtocolError("invalid service operation arguments")
            result = target(*arguments, **keywords)
            response = {
                "version": 1,
                "server_identity": self.identity,
                "request_id": request_id,
                "ok": True,
                "result": _encode(result),
            }
            status = 200
        except Exception as exc:  # noqa: BLE001 - translated across trust boundary
            response = {
                "version": 1,
                "server_identity": self.identity,
                "request_id": request_id,
                "ok": False,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "may_have_dispatched": getattr(exc, "may_have_dispatched", False)
                is True,
                "may_have_sent": getattr(exc, "may_have_sent", False) is True,
                "operation_started": getattr(exc, "operation_started", False) is True,
            }
            status = (
                403
                if isinstance(exc, (PermissionError, ServiceAuthenticationError))
                else 400
            )
        if response_secret is None:
            handler.send_error(status)
            return
        payload = _signed_frame(response, response_secret)
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def _admit(self, frame: Mapping[str, object]) -> None:
        if frame.get("version") != 1 or frame.get("server_identity") != self.identity:
            raise ServiceAuthenticationError("service request identity did not match")
        if frame.get("client_identity") not in self.allowed_client_identities:
            raise ServiceAuthenticationError("service client identity is not allowed")
        issued_at = frame.get("issued_at")
        request_id = frame.get("request_id")
        if (
            not isinstance(issued_at, int)
            or abs(int(time.time()) - issued_at) > MAX_CLOCK_SKEW_SECONDS
        ):
            raise ServiceAuthenticationError(
                "service request is outside the clock window"
            )
        if not isinstance(request_id, str) or len(request_id) != 32:
            raise ServiceAuthenticationError("service request identifier is invalid")
        with self._admission_lock:
            oldest = int(time.time()) - MAX_CLOCK_SKEW_SECONDS
            self._seen_requests = {
                key: timestamp
                for key, timestamp in self._seen_requests.items()
                if timestamp >= oldest
            }
            if request_id in self._seen_requests:
                raise ServiceAuthenticationError("service request was replayed")
            self._seen_requests[request_id] = issued_at


class RemoteAuditBoundary(AuditBoundary):
    """Audit port adapter backed by the authenticated owned-service seam."""

    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def append(self, evidence: AuditEvidence) -> None:
        try:
            self._client.call("append", evidence)
        except ServiceProtocolError as exc:
            raise AuditWriteError("audit service is unavailable") from exc

    def append_batch(self, evidence: Sequence[AuditEvidence]) -> None:
        try:
            self._client.call("append_batch", tuple(evidence))
        except ServiceProtocolError as exc:
            raise AuditWriteError("audit service is unavailable") from exc

    def safe_view(self, query: AuditFilter | None = None) -> tuple[AuditEvidence, ...]:
        result = self._client.call("safe_view", query)
        if not isinstance(result, tuple) or not all(
            isinstance(item, AuditEvidence) for item in result
        ):
            raise ServiceProtocolError("audit service returned an invalid safe view")
        return result

    def export_json(self, query: AuditFilter | None = None) -> str:
        result = self._client.call("export_json", query)
        if not isinstance(result, str):
            raise ServiceProtocolError("audit service returned an invalid export")
        return result


class RemoteOrchestrationAdapter(OrchestrationAdapter):
    """Planner port adapter backed by the authenticated owned-service seam."""

    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def run(self, request: models.OrchestrationRequest) -> models.OrchestrationResult:
        try:
            result = self._client.call("run", request)
        except ServiceProtocolError as exc:
            raise OrchestrationAdapterError(str(exc)) from exc
        if not isinstance(result, models.OrchestrationResult):
            raise OrchestrationAdapterError(
                "orchestration service returned an invalid result"
            )
        return result

    def cancel(self, *, request_id: str) -> bool:
        try:
            result = self._client.call("cancel", request_id=request_id)
        except ServiceProtocolError as exc:
            raise OrchestrationAdapterError(str(exc)) from exc
        if not isinstance(result, bool):
            raise OrchestrationAdapterError(
                "orchestration service returned an invalid cancellation result"
            )
        return result


class RemoteOutboundConnector(OutboundConnector):
    """Outbound port adapter backed by the authenticated owned-service seam."""

    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def preflight(self, reply: models.OutboundReply) -> None:
        try:
            self._client.call("preflight", reply)
        except ServiceProtocolError as exc:
            raise OutboundConnectorError(str(exc), may_have_sent=False) from exc

    def send(self, reply: models.OutboundReply) -> models.OutboundDelivery:
        try:
            result = self._client.call("send", reply)
        except ServiceProtocolError as exc:
            may_have_sent = (
                exc.may_have_sent if isinstance(exc, RemoteServiceError) else True
            )
            raise OutboundConnectorError(str(exc), may_have_sent=may_have_sent) from exc
        if not isinstance(result, models.OutboundDelivery):
            raise OutboundConnectorError(
                "outbound service returned an invalid delivery",
                may_have_sent=True,
            )
        return result


class RemoteMessagingReadinessProvider(MessagingGatewayReadinessProvider):
    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def current(self) -> MessagingGatewayReadiness:
        try:
            result = self._client.call("current")
        except ServiceProtocolError as exc:
            raise OutboundConnectorError(str(exc), may_have_sent=False) from exc
        if not isinstance(result, MessagingGatewayReadiness):
            raise OutboundConnectorError(
                "messaging service returned invalid readiness", may_have_sent=False
            )
        return result


class OwnedActionService:
    """Keep prepared connector handles inside their owning service process."""

    def __init__(self, dispatcher: ActionDispatcher) -> None:
        self._dispatcher = dispatcher
        self._prepared: dict[str, ActionDispatchHandle] = {}
        self._lock = RLock()

    def operations(self) -> Mapping[str, Callable[..., object]]:
        operations: dict[str, Callable[..., object]] = {
            "action_prepare": self.prepare,
            "action_run": self.run,
            "action_cancel": self.cancel,
            "action_finalize": self.finalize,
        }
        if isinstance(self._dispatcher, BoundActionLifecycle):
            operations["action_bind"] = self._dispatcher.bind_proposal
            operations["action_validate"] = self._dispatcher.validate_pending_action
        return operations

    def prepare(self, action: models.FrozenActionProposal) -> None:
        with self._lock:
            if action.action_id in self._prepared:
                raise ActionDispatcherError(
                    "action is already prepared", may_have_dispatched=True
                )
            self._prepared[action.action_id] = self._dispatcher.prepare(action)

    def run(self, action_id: str) -> object | None:
        with self._lock:
            handle = self._prepared.get(action_id)
        if handle is None:
            raise ActionDispatcherError(
                "prepared action is unavailable", may_have_dispatched=True
            )
        return handle.run()

    def cancel(self, action_id: str) -> ActionCancellationResult:
        return self._dispatcher.cancel(action_id=action_id)

    def finalize(self, action_id: str) -> None:
        with self._lock:
            self._prepared.pop(action_id, None)
        if isinstance(self._dispatcher, ActionFinalizer):
            self._dispatcher.finalize(action_id=action_id)


class _RemoteActionHandle(ActionDispatchHandle):
    def __init__(self, client: AuthenticatedServiceClient, action_id: str) -> None:
        self._client = client
        self._action_id = action_id

    def run(self) -> object | None:
        try:
            return self._client.call("action_run", self._action_id)
        except ServiceProtocolError as exc:
            may_have_dispatched = (
                exc.may_have_dispatched if isinstance(exc, RemoteServiceError) else True
            )
            raise ActionDispatcherError(
                str(exc), may_have_dispatched=may_have_dispatched
            ) from exc

    def cancel(self) -> ActionCancellationResult:
        try:
            result = self._client.call("action_cancel", self._action_id)
        except (RemoteServiceError, ServiceProtocolError):
            return ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        return (
            result
            if isinstance(result, ActionCancellationResult)
            else ActionCancellationResult(ActionCancellationStatus.UNKNOWN)
        )


class RemoteActionDispatcher(ActionDispatcher, BoundActionLifecycle, ActionFinalizer):
    """Prepared action port whose execution handle remains in the owner process."""

    def __init__(
        self, client: AuthenticatedServiceClient, *, bound: bool = False
    ) -> None:
        self._client = client
        self._bound = bound

    def prepare(self, action: models.FrozenActionProposal) -> ActionDispatchHandle:
        try:
            self._client.call("action_prepare", action)
        except ServiceProtocolError as exc:
            may_have_dispatched = (
                exc.may_have_dispatched if isinstance(exc, RemoteServiceError) else True
            )
            raise ActionDispatcherError(
                str(exc), may_have_dispatched=may_have_dispatched
            ) from exc
        return _RemoteActionHandle(self._client, action.action_id)

    def cancel(self, *, action_id: str) -> ActionCancellationResult:
        return _RemoteActionHandle(self._client, action_id).cancel()

    def finalize(self, *, action_id: str) -> None:
        try:
            self._client.call("action_finalize", action_id)
        except ServiceProtocolError:
            return

    def bind_proposal(
        self, action: models.FrozenActionProposal
    ) -> models.FrozenActionProposal:
        if not self._bound:
            return action
        try:
            result = self._client.call("action_bind", action)
        except RemoteServiceError as exc:
            raise ActionDispatcherError(str(exc)) from exc
        if not isinstance(result, models.FrozenActionProposal):
            raise ActionDispatcherError("action owner returned an invalid binding")
        return result

    def validate_pending_action(self, action: models.FrozenActionProposal) -> None:
        if not self._bound:
            return
        try:
            self._client.call("action_validate", action)
        except RemoteServiceError as exc:
            raise ActionDispatcherError(str(exc)) from exc


class RemoteVaultProposalPreparer(KnowledgeVaultWriteProposalPreparer):
    def __init__(self, client: AuthenticatedServiceClient) -> None:
        self._client = client

    def propose(
        self, *, request_id: str, changes: Mapping[str, str]
    ) -> models.FrozenActionProposal:
        try:
            result = self._client.call(
                "propose", request_id=request_id, changes=dict(changes)
            )
        except RemoteServiceError as exc:
            raise ActionDispatcherError(str(exc)) from exc
        if not isinstance(result, models.FrozenActionProposal):
            raise ActionDispatcherError("vault service returned an invalid proposal")
        return result


def find_available_port() -> int:
    """Reserve and release one loopback port for isolated process tests."""

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_until_ready(host: str, port: int, *, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://{host}:{port}/health", timeout=0.2) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            time.sleep(0.02)
    raise TimeoutError("owned service did not become ready")
