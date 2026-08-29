"""Authenticated, bounded protocol for owned Jarvis service seams.

The protocol deliberately exposes named operations instead of remote object or
module access.  Both peers authenticate every frame and the server keeps a
short replay window.  The typed codec accepts only the closed Jarvis model
registry; it never imports a type named by an incoming frame.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
        code: str | None = None,
        may_have_dispatched: bool = False,
        may_have_sent: bool = False,
        operation_started: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.code = code
        self.may_have_dispatched = may_have_dispatched
        self.may_have_sent = may_have_sent
        self.operation_started = operation_started


from .service_codec import (  # noqa: F401
    _TYPES,
    _canonical_json,
    _decode,
    _encode,
    _peek_client_identity,
    _sign,
    _signed_frame,
    _type_registry,
    _verify_frame,
)


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
                code=(
                    response_frame.get("error_code")
                    if isinstance(response_frame.get("error_code"), str)
                    else None
                ),
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
            error_code = getattr(exc, "code", None)
            response = {
                "version": 1,
                "server_identity": self.identity,
                "request_id": request_id,
                "ok": False,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "error_code": error_code if isinstance(error_code, str) else None,
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


from .service_adapters import (  # noqa: F401
    OwnedActionService,
    RemoteActionDispatcher,
    RemoteAuditBoundary,
    RemoteGoogleReadinessProvider,
    RemoteMessagingReadinessProvider,
    RemoteOrchestrationAdapter,
    RemoteOutboundConnector,
    RemoteVaultProposalPreparer,
    RemoteWorkerReadinessProvider,
    _RemoteActionHandle,
    find_available_port,
    wait_until_ready,
)
