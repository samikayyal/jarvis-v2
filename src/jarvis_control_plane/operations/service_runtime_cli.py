"""Command-line parser and dispatcher for the service runtime."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any


def parser(*, runtime: Any) -> Any:
    parser = runtime.argparse.ArgumentParser(description=runtime.__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    serve_parser = subcommands.add_parser("serve")
    serve_parser.add_argument("role", choices=tuple(runtime.SERVICE_ROLES))
    serve_parser.add_argument(
        "--configuration", type=Path, default=Path("/run/jarvis/config.toml")
    )
    serve_parser.add_argument(
        "--protocol-root", type=Path, default=Path("/run/protocol")
    )
    subcommands.add_parser("health")
    proxy_parser = subcommands.add_parser("serve-egress-proxy")
    proxy_parser.add_argument("kind", choices=("orchestration", "google", "vault"))
    proxy_parser.add_argument("--port", type=int, default=9080)
    proxy_parser.add_argument(
        "--configuration", type=Path, default=Path("/run/jarvis/config.toml")
    )
    proxy_health = subcommands.add_parser("proxy-health")
    proxy_health.add_argument("--port", type=int, default=9080)
    connect_parser = subcommands.add_parser("egress-connect")
    connect_parser.add_argument("target_host")
    connect_parser.add_argument("target_port", type=int)
    connect_parser.add_argument("--proxy-host", required=True)
    connect_parser.add_argument("--proxy-port", type=int, default=9080)
    authorize_parser = subcommands.add_parser("google-authorize")
    authorize_parser.add_argument("--operation-id", required=True)
    authorize_parser.add_argument(
        "--access",
        choices=tuple(runtime._GOOGLE_AUTHORIZATION_ACCESS_SCOPES),
        default="baseline",
    )
    authorize_parser.add_argument(
        "--configuration", type=Path, default=Path("/run/jarvis/config.toml")
    )
    disconnect_parser = subcommands.add_parser("google-disconnect")
    disconnect_parser.add_argument(
        "--configuration", type=Path, default=Path("/run/jarvis/config.toml")
    )
    for command in ("audit-view", "audit-export"):
        audit_parser = subcommands.add_parser(command)
        audit_parser.add_argument(
            "--configuration", type=Path, default=Path("/run/jarvis/config.toml")
        )
    status_parser = subcommands.add_parser("admin-status")
    status_parser.add_argument(
        "--configuration", type=Path, default=Path("/run/jarvis/config.toml")
    )
    status_parser.add_argument(
        "--artifact-lock",
        type=Path,
        default=Path("/opt/jarvis/deployment/artifacts.lock.json"),
    )
    return parser


def main(argv: Sequence[str] | None = None, *, runtime: Any) -> int:
    arguments = parser(runtime=runtime).parse_args(argv)
    if arguments.command == "serve-egress-proxy":
        expected_identity = f"jarvis-{arguments.kind}-egress"
        if runtime.os.environ.get("JARVIS_SERVICE_IDENTITY") != expected_identity:
            raise runtime.CompositionError(
                "egress proxy identity does not match its role"
            )
        configuration = runtime._load_configuration(arguments.configuration)
        egress = configuration.get("egress")
        if not isinstance(egress, runtime.Mapping):
            raise runtime.CompositionError("egress proxy configuration is unavailable")
        hosts = egress.get(f"{arguments.kind}_hosts")
        if not isinstance(hosts, list) or not all(
            isinstance(item, str) for item in hosts
        ):
            raise runtime.CompositionError("egress proxy host allowlist is invalid")
        runtime.serve_egress_proxy(
            host="0.0.0.0",
            port=arguments.port,
            allowed_hosts=hosts,
            allowed_ports=(22,) if arguments.kind == "vault" else (443,),
        )
    elif arguments.command == "proxy-health":
        try:
            with runtime.socket.create_connection(
                ("127.0.0.1", arguments.port), timeout=2
            ) as probe:
                probe.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
                response = probe.recv(128)
            if not response.startswith(b"HTTP/1.1 200 "):
                raise runtime.CompositionError(
                    "egress proxy health response is invalid"
                )
        except OSError as exc:
            raise runtime.CompositionError("egress proxy is not healthy") from exc
    elif arguments.command == "egress-connect":
        runtime.connect_through_proxy(
            proxy_host=arguments.proxy_host,
            proxy_port=arguments.proxy_port,
            target_host=arguments.target_host,
            target_port=arguments.target_port,
        )
    elif arguments.command == "health":
        runtime.health()
    elif arguments.command in {"audit-view", "audit-export"}:
        if runtime.os.environ.get("JARVIS_SERVICE_IDENTITY") != "jarvis-audit":
            raise runtime.CompositionError(
                "audit administration requires the audit identity"
            )
        configuration = runtime._load_configuration(arguments.configuration)
        paths = configuration.get("paths")
        if not isinstance(paths, runtime.Mapping):
            raise runtime.CompositionError("audit path configuration is unavailable")
        audit_root = runtime.Path(
            runtime._require_text(paths.get("audit"), "paths.audit")
        )
        audit = runtime.SQLiteAuditBoundary(audit_root / "audit.sqlite3")
        if arguments.command == "audit-view":
            print(
                runtime.json.dumps(
                    runtime._encode(audit.safe_view()), separators=(",", ":")
                )
            )
        else:
            print(audit.export_json())
    elif arguments.command in {"google-authorize", "google-disconnect"}:
        if runtime.os.environ.get("JARVIS_SERVICE_IDENTITY") != "jarvis-broker":
            raise runtime.CompositionError(
                "Google administration requires the broker identity"
            )
        configuration = runtime._load_configuration(arguments.configuration)
        client = runtime._client(
            configuration,
            client_identity="jarvis-broker",
            server_role="google_connector",
        )
        if arguments.command == "google-authorize":
            requested_scopes = (
                runtime.GOOGLE_OAUTH_BASELINE_SCOPES
                | runtime._GOOGLE_AUTHORIZATION_ACCESS_SCOPES[arguments.access]
            )
            result = client.call(
                "start_authorization",
                operation_id=arguments.operation_id,
                requested_scopes=tuple(sorted(requested_scopes)),
            )
            if not isinstance(result, str) or not result.startswith(
                "https://accounts.google.com/"
            ):
                raise runtime.CompositionError(
                    "Google authorization URL was unavailable"
                )
            print(result)
        else:
            client.call("disconnect")
            print("Google connection disconnected.")
    elif arguments.command == "admin-status":
        if runtime.os.environ.get("JARVIS_SERVICE_IDENTITY") != "jarvis-broker":
            raise runtime.CompositionError(
                "administrative status requires the broker identity"
            )
        configuration = runtime._load_configuration(arguments.configuration)
        print(
            runtime.json.dumps(
                runtime.administrative_status(
                    configuration, artifact_lock_path=arguments.artifact_lock
                ),
                sort_keys=True,
            )
        )
    else:
        runtime.serve(
            arguments.role,
            configuration_path=arguments.configuration,
            protocol_root=arguments.protocol_root,
        )
    return 0
