"""Small production health probe that does not import the control plane."""

from __future__ import annotations

import os
import socket
import stat

HTTP_PORTS = {
    "jarvis-inbound": 9011,
    "jarvis-broker": 9012,
    "jarvis-orchestration": 9013,
    "jarvis-audit": 9014,
    "jarvis-google": 9015,
    "jarvis-vault": 9016,
    "jarvis-openwa-outbound": 9017,
    "jarvis-worker-gateway": 9018,
    "jarvis-oauth-callback": 8080,
    "jarvis-orchestration-egress": 9080,
    "jarvis-google-egress": 9080,
    "jarvis-vault-egress": 9080,
}


def deleted_archive_ready(endpoint: str = "/run/jarvis-deleted/writer.sock") -> None:
    if not stat.S_ISSOCK(os.stat(endpoint).st_mode):
        raise SystemExit("deleted archive socket is unavailable")


def main() -> None:
    identity = os.environ.get("JARVIS_SERVICE_IDENTITY", "")
    if identity == "jarvis-deleted-archive":
        deleted_archive_ready()
        return
    port = HTTP_PORTS.get(identity)
    if port is None:
        raise SystemExit("unknown service identity")
    with socket.create_connection(("127.0.0.1", port), timeout=2) as probe:
        probe.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        response = probe.recv(128)
    if not response.startswith((b"HTTP/1.0 200 ", b"HTTP/1.1 200 ")):
        raise SystemExit("service is not healthy")


if __name__ == "__main__":
    main()
