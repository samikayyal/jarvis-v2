"""HTTP service handlers and the role-specific service entrypoint."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import urlopen

from ..models import SignedInboundEvent


def verified_inbound_event(
    raw_body: bytes, signature: str | None, signing_secret: bytes
) -> SignedInboundEvent | None:
    event = SignedInboundEvent(raw_body=raw_body, signature=signature)
    return event if event.verify(signing_secret) else None


def serve_inbound_receiver(
    config: Mapping[str, object], protocol_root: Path, *, runtime: object
) -> None:
    credential = runtime._credential_json(  # type: ignore[attr-defined]
        runtime.Path("/run/credentials/openwa-inbound/credentials.json")  # type: ignore[attr-defined]
    )
    signing_secret = runtime._require_text(  # type: ignore[attr-defined]
        credential.get("openwa_signing_secret"), "OpenWA signing secret"
    ).encode()
    broker = runtime.AuthenticatedServiceClient(  # type: ignore[attr-defined]
        identity="jarvis-inbound",
        expected_server_identity="jarvis-broker",
        secret=runtime._read_secret(  # type: ignore[attr-defined]
            protocol_root / "inbound_receiver--capability_broker.key"
        ),
        host="capability_broker",
        port=runtime.SERVICE_ROLES["capability_broker"].port,  # type: ignore[attr-defined]
        operation_timeouts=runtime._operation_timeouts(  # type: ignore[attr-defined]
            config, server_role="capability_broker"
        ),
    )

    class Handler(runtime.BaseHTTPRequestHandler):  # type: ignore[attr-defined,misc]
        server_version = "JarvisInboundReceiver/1"

        def do_GET(self) -> None:
            if self.path != "/health":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self) -> None:
            if self.path != "/webhook":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 128 * 1024:
                self.send_error(413)
                return
            signatures = self.headers.get_all("X-OpenWA-Signature") or []
            signature = signatures[0] if len(signatures) == 1 else None
            raw_body = self.rfile.read(length)
            event = runtime._verified_inbound_event(  # type: ignore[attr-defined]
                raw_body, signature, signing_secret
            )
            if event is None:
                payload = b'{"disposition":"unauthenticated"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            try:
                result = broker.call("receive", event)
                status = getattr(result, "status_code", 503)
                payload = json.dumps(
                    {
                        "disposition": getattr(result, "disposition", "unavailable"),
                        "reason": getattr(result, "reason", None),
                    },
                    separators=(",", ":"),
                ).encode()
            except Exception:  # noqa: BLE001 - private upstream fails closed
                status = 503
                payload = b'{"disposition":"unavailable"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = runtime.ThreadingHTTPServer(  # type: ignore[attr-defined]
        ("0.0.0.0", runtime.SERVICE_ROLES["inbound_receiver"].port),
        Handler,  # type: ignore[attr-defined]
    )
    server.daemon_threads = True
    server.serve_forever()


def serve_oauth_callback(
    config: Mapping[str, object], protocol_root: Path, *, runtime: object
) -> None:
    google = runtime.AuthenticatedServiceClient(  # type: ignore[attr-defined]
        identity="jarvis-oauth-callback",
        expected_server_identity="jarvis-google",
        secret=runtime._read_secret(  # type: ignore[attr-defined]
            protocol_root / "public_oauth_callback--google_connector.key"
        ),
        host="google_connector",
        port=runtime.SERVICE_ROLES["google_connector"].port,  # type: ignore[attr-defined]
        operation_timeouts=runtime._operation_timeouts(  # type: ignore[attr-defined]
            config, server_role="google_connector"
        ),
    )

    class Handler(runtime.BaseHTTPRequestHandler):  # type: ignore[attr-defined,misc]
        server_version = "JarvisOAuthCallback/1"

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                return
            if parsed.path != "/callback":
                self.send_error(404)
                return
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            if len({name for name, _value in pairs}) != len(pairs):
                self._empty_response(400)
                return
            try:
                result = google.call("oauth_callback", method="GET", query=dict(pairs))
                status = getattr(result, "status_code", 503)
                headers = getattr(result, "headers", {})
            except Exception:  # noqa: BLE001 - private upstream fails closed
                status = 503
                headers = {}
            self.send_response(status)
            for name in ("Cache-Control", "Referrer-Policy"):
                value = headers.get(name) if isinstance(headers, Mapping) else None
                if isinstance(value, str):
                    self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            self._empty_response(405)

        def _empty_response(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = runtime.ThreadingHTTPServer(  # type: ignore[attr-defined]
        ("0.0.0.0", runtime.SERVICE_ROLES["public_oauth_callback"].port),
        Handler,  # type: ignore[attr-defined]
    )
    server.daemon_threads = True
    server.serve_forever()


def serve(
    role_name: str, *, configuration_path: Path, protocol_root: Path, runtime: object
) -> None:
    role = runtime.SERVICE_ROLES.get(role_name)  # type: ignore[attr-defined]
    if role is None:
        raise runtime.CompositionError("unknown service role")  # type: ignore[attr-defined]
    expected_identity = os.environ.get("JARVIS_SERVICE_IDENTITY")
    if expected_identity != role.identity:
        raise runtime.CompositionError(  # type: ignore[attr-defined]
            "runtime service identity does not match its role"
        )
    configuration = runtime._load_configuration(configuration_path)  # type: ignore[attr-defined]
    identities = configuration.get("identities")
    if (
        not isinstance(identities, Mapping)
        or identities.get(role_name) != role.identity
    ):
        raise runtime.CompositionError(  # type: ignore[attr-defined]
            "configured service identity does not match its role"
        )
    if role_name == "inbound_receiver":
        serve_inbound_receiver(configuration, protocol_root, runtime=runtime)
        return
    if role_name == "public_oauth_callback":
        serve_oauth_callback(configuration, protocol_root, runtime=runtime)
        return
    if role_name == "deleted_conversation_archive":
        paths = configuration.get("paths")
        if not isinstance(paths, Mapping):
            raise runtime.CompositionError(  # type: ignore[attr-defined]
                "deleted archive configuration is incomplete"
            )
        archive_root = runtime.Path(  # type: ignore[attr-defined]
            runtime._require_text(  # type: ignore[attr-defined]
                paths.get("deleted_conversations"), "paths.deleted_conversations"
            )
        )
        archive_root.mkdir(parents=True, exist_ok=True)
        runtime.serve_sqlite_deleted_conversation_archive(  # type: ignore[attr-defined]
            archive_root / "deleted-conversations.sqlite3",
            "/run/jarvis-deleted/writer.sock",
            authkey=runtime._read_secret(  # type: ignore[attr-defined]
                protocol_root / "capability_broker--deleted_conversation_archive.key"
            ),
        )
        return
    operations = runtime.build_operations(role_name, configuration)  # type: ignore[attr-defined]
    allowed_identities, operation_allowlists = runtime._service_access(  # type: ignore[attr-defined]
        role_name, role
    )
    client_secrets: dict[str, bytes] = {}
    for client_identity in allowed_identities:
        client_role = next(
            item.name
            for item in runtime.SERVICE_ROLES.values()  # type: ignore[attr-defined]
            if item.identity == client_identity
        )
        client_secrets[client_identity] = runtime._read_secret(  # type: ignore[attr-defined]
            protocol_root / f"{client_role}--{role_name}.key"
        )
    runtime.AuthenticatedServiceServer(  # type: ignore[attr-defined]
        identity=role.identity,
        client_secrets=client_secrets,
        host="0.0.0.0",
        port=role.port,
        operations=operations,
        allowed_client_identities=allowed_identities,
        allowed_operations_by_client=operation_allowlists,
    ).serve_forever()


def health(*, runtime: object) -> None:
    identity = os.environ.get("JARVIS_SERVICE_IDENTITY")
    role = next(
        (item for item in runtime.SERVICE_ROLES.values() if item.identity == identity),  # type: ignore[attr-defined]
        None,
    )
    if role is None:
        raise runtime.CompositionError("runtime service identity is unknown")  # type: ignore[attr-defined]
    if role.name == "deleted_conversation_archive":
        writer = runtime.SQLiteDeletedConversationArchiveWriter(  # type: ignore[attr-defined]
            "/run/jarvis-deleted/writer.sock",
            authkey=runtime._read_secret(  # type: ignore[attr-defined]
                runtime.Path("/run/protocol")  # type: ignore[attr-defined]
                / "capability_broker--deleted_conversation_archive.key"
            ),
        )
        writer.close()
        return
    try:
        with urlopen(f"http://127.0.0.1:{role.port}/health", timeout=2) as response:
            if response.status != 200 or response.read(3) != b"ok":
                raise runtime.CompositionError(  # type: ignore[attr-defined]
                    "service health response is invalid"
                )
    except (OSError, URLError) as exc:
        raise runtime.CompositionError("service is not healthy") from exc  # type: ignore[attr-defined]
